"""Legge il nome della partita dal riquadro del punteggio in alto a sinistra del video.

Fotogramma (dall'HLS se il canale è già aperto, altrimenti da un solo pezzo della diretta) → ritaglio in alto
a sinistra → riconoscimento del testo (Vision di macOS, oppure Tesseract) → abbinamento con le partite di oggi.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

import config

log = logging.getLogger("scorebug")

CROP = "crop=trunc(iw*0.5/2)*2:trunc(ih*0.22/2)*2:0:0"      # riquadro in alto a sinistra
VISION_BIN = config.DATA_DIR / "ocr-vision"
VISION_SRC = config.BASE_DIR / "tools" / "ocr_vision.swift"
_engine: str | None = None                                  # "vision", "tesseract" o "" (nessuno)
_engine_lock = asyncio.Lock()

WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-:' "
STOP = {"FC", "AC", "AS", "SS", "US", "CF", "SC", "SSC", "ACF", "AFC", "CALCIO", "CLUB", "DE", "DI", "LA", "EL", "THE",
        "REAL", "UNITED", "CITY", "SPORTING", "ATLETICO", "1907", "1913", "1909", "LIVE", "HD", "FT", "HT", "BET", "TV",
        "AND", "VAR", "GOL", "GOAL", "ADD", "REC", "HOVE"}


async def _run(*cmd: str, timeout: float = 30) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, b"", b"timeout"
    return proc.returncode, out, err


async def engine() -> str:
    """Sceglie il motore una volta sola: Vision (compilato al primo uso) oppure Tesseract."""
    global _engine
    async with _engine_lock:
        if _engine is not None:
            return _engine
        if VISION_BIN.exists():
            _engine = "vision"
        elif shutil.which("swiftc") and VISION_SRC.exists():
            log.info("Preparo il riconoscimento testo di macOS (una volta sola, ~30 s)…")
            code, _, err = await _run("swiftc", "-O", str(VISION_SRC), "-o", str(VISION_BIN), timeout=300)
            if code == 0:
                _engine = "vision"
            else:
                log.info("Vision non disponibile: %s", err.decode(errors="replace")[-300:])
        if _engine is None:
            _engine = "tesseract" if shutil.which("tesseract") else ""
        log.info("Lettura nome partita dal video: %s", _engine or "non disponibile (installa tesseract: brew install tesseract)")
        return _engine


# --------------------------------------------------------------------------- fotogramma

async def crop_frame(src: Path, out: Path, negate: bool = False) -> bool:
    """Ritaglio in alto a sinistra di 3 fotogrammi distanti ~0,7 s, uno sotto l'altro in un'unica immagine:
    se in uno il riquadro è coperto o sfumato, negli altri di solito si legge."""
    vf = ("select='not(mod(n\\,18))'," + CROP + ",scale=iw*2:-2:flags=lanczos," + ("format=gray,negate," if negate else "")
          + "tile=1x3:padding=6")
    code, _, err = await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                              "-vf", vf, "-frames:v", "1", "-vsync", "vfr", str(out))
    if code != 0 or not out.exists():                  # pezzo troppo corto per 3 fotogrammi: uno solo
        vf1 = CROP + ",scale=iw*3:-2:flags=lanczos" + (",format=gray,negate" if negate else "")
        code, _, err = await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                                  "-frames:v", "1", "-vf", vf1, str(out))
    if code != 0 or not out.exists():
        log.info("Fotogramma non estratto: %s", err.decode(errors="replace")[-200:])
        return False
    return True


def latest_segment(hls_dir: Path) -> Path | None:
    segs = sorted(hls_dir.glob("seg*.ts"), key=lambda p: p.stat().st_mtime)
    return segs[-2] if len(segs) >= 2 else (segs[-1] if segs else None)   # il penultimo è già completo


async def read_text(src: Path) -> str:
    """Testo del riquadro in alto a sinistra di un file video (segmento .ts o pezzo .mp4)."""
    eng = await engine()
    if not eng:
        return ""
    tmp = Path(tempfile.mkdtemp(prefix="cs-ocr-"))
    try:
        if eng == "vision":
            png = tmp / "f.png"
            if not await crop_frame(src, png):
                return ""
            code, out, _ = await _run(str(VISION_BIN), str(png))
            return out.decode(errors="replace") if code == 0 else ""
        texts = []
        png = tmp / "f.png"                    # testo chiaro su fondo scuro (il caso tipico): lo inverto per Tesseract
        if not await crop_frame(src, png, negate=True):
            return ""
        for psm in ("6", "11"):                # riga di testo e testo sparso
            code, out, _ = await _run("tesseract", str(png), "stdout", "--psm", psm, "-c", f"tessedit_char_whitelist={WHITELIST}")
            if code == 0:
                texts.append(out.decode(errors="replace"))
        return "\n".join(texts)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- abbinamento

# Lettere cirilliche e greche che il riconoscimento confonde con quelle latine (MСI con la С russa, ETIНAD…).
HOMOGLYPHS = str.maketrans("АВСЕНІЈКМОРЅТХУаВсеНіјкмоорѕтхуЬьΑΒΕΗΙΚΜΝΟΡΤΧΥΖ", "ABCEHIJKMOPSTXYaBceHijkmoopstxybbABEHIKMNOPTXYZ")


def fix_digits(s: str) -> str:
    """O letta al posto di 0 vicino al punteggio (1-O → 1-0)."""
    s = (s or "").translate(HOMOGLYPHS)
    return re.sub(r"(?<=\d-)O|(?<=\d - )O|O(?=\s?-\s?\d)", "0", s)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", fix_digits(s)).encode("ascii", "ignore").decode().upper()
    s = re.sub(r"(?<=[A-Z])(?=\d)|(?<=\d)(?=[A-Z])", " ", s)       # CRO1-1CAS → CRO 1-1 CAS
    return re.sub(r"[^A-Z0-9]+", " ", s).strip()


def team_keys(names: list[str]) -> tuple[set[str], set[str]]:
    """(parole intere, sigle da 3 lettere) che possono comparire nel riquadro per una squadra.
    Sigle: quelle ufficiali, prime 3 lettere, iniziali (Brighton & Hove Albion → BHA),
    lettera + sillaba (Man City → MCI, Real Madrid → RMA), prima e due consonanti (Bologna → BLG)."""
    words, abbr = set(), set()
    for n in names:
        nn = norm(n)
        if not nn:
            continue
        if 2 <= len(nn) <= 4 and " " not in nn:
            abbr.add(nn)
        parts = [w for w in nn.split() if not w.isdigit()]
        core = [w for w in parts if w not in STOP] or parts
        for w in core:
            if len(w) >= 4:
                words.add(w)
            if len(w) >= 3:
                abbr.add(w[:3])
                cons = w[0] + "".join(ch for ch in w[1:] if ch not in "AEIOU")
                if len(cons) >= 3:
                    abbr.add(cons[:3])
        if len(parts) >= 2:
            ini = "".join(w[0] for w in parts if w)
            if len(ini) >= 3:
                abbr.add(ini[:3])
            a, b = parts[0], parts[1]
            if len(b) >= 2:
                abbr.add(a[0] + b[:2])
            if len(a) >= 2:
                abbr.add(a[:2] + b[0])
    return words, abbr


def _similar(t: str, w: str) -> bool:
    from difflib import SequenceMatcher
    if t == w:
        return True
    if len(t) >= 5 and (t in w or w in t):                       # ORENTIN in FIORENTINA
        return True
    return len(t) >= 4 and len(w) >= 4 and SequenceMatcher(None, t, w).ratio() >= 0.8


def _side_hits(tokens: list[str], names: list[str]) -> list[tuple[int, int]]:
    """[(posizione, punti)] dei pezzi di testo che somigliano a questa squadra: 3 per il nome, 2 per la sigla."""
    words, abbr = team_keys(names)
    hits = []
    for i, t in enumerate(tokens):
        if len(t) >= 4 and any(_similar(t, w) for w in words):
            hits.append((i, 3))
        elif len(t) == 3 and t in abbr:
            hits.append((i, 2))
    return hits


def find_score(text: str) -> str | None:
    text = fix_digits(text)
    t = re.sub(r"\b\d{1,3}\s*[:'.]\s*\d{2}\b", " ", text)         # via il cronometro (23:15, 45'00)
    m = re.search(r"(?<!\d)(\d{1,2})\s*[-–]\s*(\d{1,2})(?!\d)", t) or re.search(r"(?<![\d:])\b(\d)\s+(\d)\b(?![\d:])", t)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def identify(text: str, fixtures: list[dict]) -> dict | None:
    """Partita letta nel riquadro: {"title", "score", "league", "matched", "fixture_id"} oppure None.
    Le due squadre devono comparire vicine (entro 6 parole), come nel riquadro del punteggio:
    così le scritte delle pubblicità non vengono scambiate per un nome."""
    tokens = norm(text).split()
    if not tokens:
        return None
    best, best_sc = None, 0
    for m in fixtures:
        hh = _side_hits(tokens, [m.get("home", "")] + list(m.get("home_alt") or []))
        aa = _side_hits(tokens, [m.get("away", "")] + list(m.get("away_alt") or []))
        sc = 0
        for i, ph in hh:
            for j, pa in aa:
                if i != j and abs(i - j) <= 6:
                    sc = max(sc, ph + pa + (1 if i < j else 0))
        if sc >= 4 and sc > best_sc:
            best, best_sc = m, sc
    if best:
        score = None
        if best.get("home_score") not in (None, "") and best.get("away_score") not in (None, ""):
            score = f"{best['home_score']}-{best['away_score']}"
        return {"title": f"{best['home']} – {best['away']}", "score": score or find_score(text),
                "league": best.get("league"), "matched": True, "fixture_id": best.get("id")}
    # Nessuna partita in elenco: «SIG 1-0 SIG» (sigle da 3 lettere attorno al punteggio).
    raw = unicodedata.normalize("NFKD", fix_digits(text)).encode("ascii", "ignore").decode().upper()
    m = re.search(r"\b([A-Z]{3})\s*(\d{1,2})?\s*[-–]\s*(\d{1,2})\s*([A-Z]{3})\b", raw) or \
        re.search(r"\b([A-Z]{3})\s+(\d{1,2})\s+(\d{1,2})\s+([A-Z]{3})\b", raw)
    if m and m.group(1) != m.group(4) and m.group(1) not in STOP and m.group(4) not in STOP:
        sc = f"{m.group(2)}-{m.group(3)}" if m.group(2) else None
        return {"title": f"{m.group(1)} – {m.group(4)}", "score": sc, "league": None, "matched": False, "fixture_id": None}
    return None
