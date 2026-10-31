"""Server di Canali Sport: app web + API + stream HLS delle dirette Telegram.

Avvio:  python avvia.py   (l'account Telegram si collega da Impostazioni nell'app, oppure con python login.py)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import account
import config
from account import AccountLogin, events_bot
from bot_events import demo_events
from matches import today_matches
from streamer import DemoSource, SlotStreamer, TelegramSource

def _log_handlers():
    """Log anche su data/server.log (max 2 MB × 2), utile per capire scatti e problemi."""
    from logging.handlers import RotatingFileHandler
    hs = [logging.StreamHandler()]
    try:
        hs.append(RotatingFileHandler(config.DATA_DIR / "server.log", maxBytes=2_000_000, backupCount=1, encoding="utf-8"))
    except OSError:
        pass
    return hs


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=_log_handlers())
log = logging.getLogger("server")

MAX_CHANNELS = 60
MAX_STREAMS = 3          # canali diversi in riproduzione contemporanea
STATIC = config.BASE_DIR / "static"
HLS_ROOT = config.DATA_DIR / "hls"
SLOTS_FILE = config.DATA_DIR / "canali.json"
SECRET_FILE = config.DATA_DIR / "secret.key"
LIVE_CACHE_S = 45
MIN_VIEWERS = 50          # sotto questa soglia il canale è nascosto e non si carica niente
SMALL_CACHE_S = 300


class App:
    client = None          # TelegramClient
    live = None            # TelegramLive
    tg_error = ""
    streamers: dict[int, SlotStreamer] = {}
    live_cache: dict[int, tuple[float, dict]] = {}
    events = None          # BotEvents
    events_cache: tuple[float, list] | None = None
    events_error = ""
    unlock_task = None
    me = None              # utente Telegram collegato
    detected: dict[int, dict] = {}     # partita letta nel video: {cid: {title, score, league, at}}
    last_seen = 0.0                    # ultima volta che qualcuno ha aperto l'app
    snapshot = None                    # (peer_id, task) del fotogramma in corso
    last_activity = 0.0                # ultima richiesta dall'app (riposo dopo REST_AFTER_S)
    resting = False                    # True = staccato da Telegram perché nessuno usa l'app
    wake_lock = asyncio.Lock()
    login = AccountLogin()
    demo_account = {"name": "Account demo", "username": "leone_demo", "phone": "393331234567"}


# --------------------------------------------------------------------------- persistence

def load_channels() -> list[dict]:
    """Elenco dei canali: [{"id": 1, "name": "...", "peer_id": -100...}]."""
    try:
        saved = json.loads(SLOTS_FILE.read_text())
    except (OSError, ValueError):
        saved = None
    if saved is None:
        if config.DEMO:
            return [{"id": i, "name": n, "peer_id": -i} for i, n in
                    enumerate(["Serie A", "Champions", "Premier", "LaLiga", "Bundesliga", "Sport Mix"], start=1)]
        return []
    channels = []
    for i, c in enumerate(saved, start=1):
        if "id" not in c:                      # vecchio formato a 5 tasti fissi
            if c.get("peer_id") is None and not c.get("name"):
                continue
            c = {**c, "id": i}
        channels.append({"id": int(c["id"]), "name": str(c.get("name") or ""), "peer_id": c.get("peer_id")})
    return channels


def save_channels(channels: list[dict]) -> None:
    tmp = SLOTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(channels, ensure_ascii=False, indent=2))
    tmp.replace(SLOTS_FILE)


def find_channel(cid: int) -> dict | None:
    return next((c for c in load_channels() if c["id"] == cid), None)


def auth_token() -> str:
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_hex(32))
    key = SECRET_FILE.read_text().strip().encode()
    return hmac.new(key, config.APP_PASSWORD.encode(), hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    if not config.APP_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get("cs_auth", ""), auth_token())


def need_auth():
    return JSONResponse({"error": "login"}, status_code=401)


# --------------------------------------------------------------------------- telegram

def make_source(cid: int, tmp: Path):
    ch = find_channel(cid)
    if config.DEMO:
        return DemoSource(cid, tmp, (ch or {}).get("name") or f"Canale {cid}")
    return TelegramSource(App.live.reader(int(ch["peer_id"]), config.STREAM_QUALITY))


def streamer(cid: int) -> SlotStreamer:
    if cid not in App.streamers:
        App.streamers[cid] = SlotStreamer(cid, HLS_ROOT, make_source)
    return App.streamers[cid]


async def live_status(peer_id) -> dict:
    if peer_id is None:
        return {"live": None}
    if config.DEMO:
        # Spettatori finti che cambiano piano piano, per vedere l'ordinamento.
        import math
        n = abs(int(peer_id))
        viewers = int(400 / n + 60 * (1 + math.sin(time.time() / 90 + n)))
        return {"live": n != 5, "rtmp": True, "title": "Segnale di prova", "viewers": viewers if n != 5 else 0}
    if not App.live:
        return {"live": None}
    now = time.monotonic()
    cached = App.live_cache.get(peer_id)
    # Mentre una diretta è in riproduzione aggiorno lo stato più di rado (ogni 2 minuti), ma lo aggiorno.
    watching = any(s.running for s in App.streamers.values())
    ttl = LIVE_CACHE_S * 2 if watching else LIVE_CACHE_S
    if cached and (cached[1].get("viewers") or 0) < MIN_VIEWERS:
        ttl = SMALL_CACHE_S                 # canali nascosti (sotto 50 spettatori): controllo ogni 5 minuti
    if cached and now - cached[0] < ttl:
        return cached[1]
    try:
        info = await App.live.live_info(int(peer_id))
        res = {"live": False} if info is None else {
            "live": True, "rtmp": info.rtmp, "title": info.title, "viewers": info.participants}
    except Exception as e:  # noqa: BLE001
        log.warning("Stato diretta %s: %s", peer_id, e)
        res = {"live": None, "error": str(e)}
    App.live_cache[peer_id] = (now, res)
    return res


# --------------------------------------------------------------------------- routes

async def index(_: Request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


FAILED_LOGINS: dict[str, list[float]] = {}
MAX_FAILS, FAIL_WINDOW_S = 5, 15 * 60


def client_ip(request: Request) -> str:
    # Dietro Caddy l'IP vero arriva in X-Forwarded-For; il server non è esposto direttamente.
    # Uso l'ultimo indirizzo, quello aggiunto dal proxy più vicino (Funnel di Tailscale o Caddy):
    # i precedenti li può scrivere chiunque.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[-1].strip() if fwd else (request.client.host if request.client else "?")


def is_home(request: Request) -> bool:
    """True se la richiesta arriva dalla rete di casa o dal Mac stesso (non da internet tramite il Funnel)."""
    import ipaddress
    try:
        return not ipaddress.ip_address(client_ip(request)).is_global
    except ValueError:
        return False


def need_home():
    return JSONResponse({"error": "Le modifiche si fanno solo da casa, con il telefono sulla stessa rete Wi-Fi del Mac."},
                        status_code=403)


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


async def login(request: Request):
    ip = client_ip(request)
    now = time.monotonic()
    fails = [t for t in FAILED_LOGINS.get(ip, []) if now - t < FAIL_WINDOW_S]
    FAILED_LOGINS[ip] = fails
    if len(fails) >= MAX_FAILS:
        return JSONResponse({"error": "Troppi tentativi. Riprova tra 15 minuti."}, status_code=429)
    body = await request.json()
    if not config.APP_PASSWORD or hmac.compare_digest(str(body.get("password", "")), config.APP_PASSWORD):
        FAILED_LOGINS.pop(ip, None)
        resp = JSONResponse({"ok": True})
        resp.set_cookie("cs_auth", auth_token(), max_age=60 * 60 * 24 * 365, httponly=True,
                        samesite="lax", secure=is_https(request))
        return resp
    fails.append(now)
    log.warning("Password sbagliata da %s (%d/%d)", ip, len(fails), MAX_FAILS)
    await asyncio.sleep(1.5)
    return JSONResponse({"error": "Password sbagliata."}, status_code=403)


async def state(request: Request):
    if not is_authed(request):
        return need_auth()
    channels = load_channels()
    App.last_seen = time.monotonic()
    async def events_or_empty():
        try:
            return await asyncio.wait_for(get_events(), timeout=8)
        except asyncio.TimeoutError:
            return App.events_cache[1] if App.events_cache else []
    statuses, evs = await asyncio.gather(
        asyncio.gather(*(live_status(c["peer_id"]) for c in channels)), events_or_empty())
    out = []
    for c, st in zip(channels, statuses):
        stv = App.streamers.get(c["id"])
        ev = next((e for e in evs if e.get("peer_id") is not None and e["peer_id"] == c["peer_id"]), None)
        if st.get("live") is False:
            App.detected.pop(c["id"], None)
        det = App.detected.get(c["id"]) if st.get("live") else None
        out.append({**c, **st,
                    "match": {k: det.get(k) for k in ("title", "score", "league")} if det and det.get("title") else None,
                    "event": {"name": ev["name"], "time": ev.get("time"), "league": ev.get("league")} if ev else None,
                    "stream": stv.state if stv else "idle",
                    "message": stv.message if stv else ""})
    return JSONResponse({"demo": config.DEMO, "telegram_error": App.tg_error, "channels": out, "min_viewers": MIN_VIEWERS,
                         "can_edit": is_home(request)})


async def telegram_channels(request: Request):
    if not is_authed(request):
        return need_auth()
    if not is_home(request):
        return need_home()
    if config.DEMO:
        return JSONResponse({"channels": [{"peer_id": -(i + 1), "title": f"Demo {i + 1}", "kind": "demo"}
                                          for i in range(8)]})
    if not App.live:
        return JSONResponse({"error": App.tg_error or "Telegram non collegato."}, status_code=503)
    return JSONResponse({"channels": await App.live.list_channels()})


def _clean(body: dict) -> tuple[str, int | None]:
    peer = body.get("peer_id")
    return str(body.get("name") or "").strip()[:40], int(peer) if peer not in (None, "") else None


async def add_channel(request: Request):
    if not is_authed(request):
        return need_auth()
    if not is_home(request):
        return need_home()
    name, peer = _clean(await request.json())
    if peer is None:
        return JSONResponse({"error": "Scegli un canale Telegram."}, status_code=400)
    channels = load_channels()
    if len(channels) >= MAX_CHANNELS:
        return JSONResponse({"error": f"Massimo {MAX_CHANNELS} canali."}, status_code=400)
    cid = max((c["id"] for c in channels), default=0) + 1
    channels.append({"id": cid, "name": name, "peer_id": peer})
    save_channels(channels)
    return JSONResponse({"ok": True, "id": cid})


async def edit_channel(request: Request):
    if not is_authed(request):
        return need_auth()
    if not is_home(request):
        return need_home()
    cid = int(request.path_params["cid"])
    channels = load_channels()
    ch = next((c for c in channels if c["id"] == cid), None)
    if ch is None:
        return JSONResponse({"error": "Canale inesistente."}, status_code=404)
    if request.method == "DELETE":
        channels = [c for c in channels if c["id"] != cid]
    else:
        name, peer = _clean(await request.json())
        if peer is None:
            return JSONResponse({"error": "Scegli un canale Telegram."}, status_code=400)
        ch.update(name=name, peer_id=peer)
    save_channels(channels)
    if cid in App.streamers:
        await App.streamers.pop(cid).stop()
    return JSONResponse({"ok": True})


async def play(request: Request):
    if not is_authed(request):
        return need_auth()
    cid = int(request.path_params["cid"])
    ch = find_channel(cid)
    if ch is None or ch["peer_id"] is None:
        return JSONResponse({"stream": "idle", "message": "Canale non collegato."}, status_code=404)
    if not config.DEMO and not App.live:
        return JSONResponse({"stream": "error", "message": App.tg_error}, status_code=503)
    s = streamer(cid)
    if request.method == "POST" and not s.running:
        # Più dispositivi possono guardare canali diversi (fino a MAX_STREAMS): oltre, chiudo quello
        # che nessuno guarda da più tempo. Lo stesso canale su più dispositivi usa un solo download.
        others = [o for o_id, o in App.streamers.items() if o_id != cid and o.running]
        while len(others) >= MAX_STREAMS:
            oldest = min(others, key=lambda o: o.last_access)
            log.info("Troppi canali aperti: chiudo il canale %s (inattivo da più tempo)", oldest.slot)
            await oldest.stop()
            others.remove(oldest)
    if request.method == "POST" and s.state in ("idle", "offline", "error", "unsupported"):
        App.live_cache.pop(ch["peer_id"], None)
        await cancel_snapshot()
        s.start()
    s.touch()
    return JSONResponse({"stream": s.state, "message": s.message})


async def stop(request: Request):
    if not is_authed(request):
        return need_auth()
    cid = int(request.path_params["cid"])
    if cid in App.streamers:
        await App.streamers[cid].stop()
    return JSONResponse({"ok": True})


async def matches(request: Request):
    if not is_authed(request):
        return need_auth()
    return JSONResponse(await today_matches(), headers={"Cache-Control": "no-cache"})


EVENTS_CACHE_S = 90


async def get_events(refresh: bool = False) -> list[dict]:
    if not events_bot():
        return []
    if config.DEMO:
        return demo_events()
    if not App.events:
        return []
    now = time.monotonic()
    if not refresh and App.events_cache and now - App.events_cache[0] < EVENTS_CACHE_S:
        return App.events_cache[1]
    try:
        evs = await App.events.fetch(refresh=refresh)
        App.events_error = ""
    except Exception as e:  # noqa: BLE001
        log.warning("Eventi dal bot non letti: %s", e)
        App.events_error = f"Non riesco a leggere @{events_bot()}: {e}"
        return App.events_cache[1] if App.events_cache else []
    App.events_cache = (now, evs)
    return evs


def public_event(ev: dict, i: int, channels: list[dict]) -> dict:
    ch = next((c for c in channels if ev.get("peer_id") is not None and c["peer_id"] == ev["peer_id"]), None)
    return {"index": i, "name": ev["name"], "league": ev.get("league"), "time": ev.get("time"),
            "start": ev.get("start"), "kind": ev.get("kind"), "member": ev.get("member"),
            "channel_title": ev.get("channel_title"), "channel_id": ch["id"] if ch else None,
            "watchable": ev.get("kind") in ("invite", "private", "public") and (ev.get("peer_id") is not None or ev.get("kind") == "invite"),
            "url": ev.get("url") if ev.get("kind") == "web" else None}


async def events(request: Request):
    if not is_authed(request):
        return need_auth()
    refresh = request.method == "POST"
    evs = await get_events(refresh=refresh)
    channels = load_channels()
    locked = bool(App.events and App.events.locked)
    return JSONResponse({"bot": events_bot(), "error": App.events_error, "locked": locked,
                         "gate_text": App.events.gate_text if locked else "",
                         "events": [public_event(e, i, channels) for i, e in enumerate(evs)]})


async def unlock_events(request: Request):
    """Avvia lo sblocco in sottofondo (POST) e ne restituisce l'avanzamento (GET). Parte solo da un tocco nell'app."""
    if not is_authed(request):
        return need_auth()
    if request.method == "POST" and not is_home(request):
        return need_home()
    if config.DEMO:
        return await _demo_unlock(request)
    if not App.events:
        return JSONResponse({"error": App.tg_error or "Telegram non collegato."}, status_code=503)
    ev = App.events
    if request.method == "POST" and not ev.unlocking:
        ev.unlocking = True
        ev.steps = ["Avvio lo sblocco…"]
        ev.last_report = None

        async def run():
            try:
                await ev.unlock()
            except Exception as e:  # noqa: BLE001
                log.exception("Sblocco non riuscito")
                ev.steps.append(f"Errore: {e}")
                ev.unlocking = False
            App.events_cache = None
        App.unlock_task = asyncio.create_task(run())
    return JSONResponse({"running": ev.unlocking, "steps": ev.steps, "report": None if ev.unlocking else ev.last_report})


_demo_state = {"t0": 0.0}


async def _demo_unlock(request: Request):
    steps = ["Chiedo la lista al bot (/start)…", "Il bot chiede 2 passaggi.", "«CANALE 4»: entro dal link del bot…",
             "«CANALE 4»: entrato.", "«APRI IL LINK» è una pagina web: va aperta da te.",
             "Aspetto qualche secondo che il bot registri tutto…", "Premo «SBLOCCA»…", "La lista non risulta ancora sbloccata."]
    if request.method == "POST":
        _demo_state["t0"] = time.monotonic()
    n = min(len(steps), int((time.monotonic() - _demo_state["t0"]) / 0.8) + 1)
    running = n < len(steps)
    report = None if running else {"ok": False, "joined": ["Canale 4 (demo)"], "rejoined": [], "already": [], "requested": [],
                                   "bots": [], "skipped": [], "failed": [], "clicked": ["🔒 SBLOCCA 🔒"], "message": "",
                                   "manual": [{"text": "🔴 APRI IL LINK 🔴", "url": "https://example.com"}],
                                   "buttons": ["CANALE 4 → invite", "APRI IL LINK → pagina web (example.com)"]}
    return JSONResponse({"running": running, "steps": steps[:n], "report": report})


async def watch_event(request: Request):
    """Collega la partita a un canale: se serve entra nel canale (solo con join=true) e lo aggiunge all'elenco."""
    if not is_authed(request):
        return need_auth()
    if not is_home(request):
        return need_home()
    body = await request.json()
    evs = await get_events()
    i = int(body.get("index", -1))
    if not 0 <= i < len(evs):
        return JSONResponse({"error": "La lista è cambiata: aggiornala e riprova."}, status_code=409)
    ev = evs[i]
    peer = ev.get("peer_id")
    if not ev.get("member") and not config.DEMO:
        if not body.get("join"):
            return JSONResponse({"need_join": True, "channel_title": ev.get("channel_title")})
        try:
            peer = await App.events.join(ev["url"])
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"Non sono riuscito a entrare nel canale: {e}"}, status_code=502)
        App.events_cache = None
    if peer is None:
        return JSONResponse({"error": "Questo link non porta a un canale Telegram."}, status_code=400)
    channels = load_channels()
    ch = next((c for c in channels if c["peer_id"] == peer), None)
    if ch is None:
        if len(channels) >= MAX_CHANNELS:
            return JSONResponse({"error": f"Massimo {MAX_CHANNELS} canali."}, status_code=400)
        cid = max((c["id"] for c in channels), default=0) + 1
        ch = {"id": cid, "name": (ev.get("channel_title") or ev["name"])[:40], "peer_id": peer}
        channels.append(ch)
        save_channels(channels)
    return JSONResponse({"ok": True, "id": ch["id"]})


async def hls(request: Request):
    if not is_authed(request):
        return Response(status_code=401)
    cid = int(request.path_params["cid"])
    name = request.path_params["file"]
    if cid not in App.streamers or "/" in name or ".." in name:
        return Response(status_code=404)
    s = App.streamers[cid]
    s.touch()
    path = s.dir / name
    if not path.exists():
        return Response(status_code=404)
    cors = {"Access-Control-Allow-Origin": "*"}   # Chromecast legge il flusso direttamente dal server
    if name.endswith(".m3u8"):
        return Response(path.read_bytes(), media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache, no-store", **cors})
    return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "max-age=60", **cors})


# --------------------------------------------------------------------------- nome della partita dal video

RETRY_S = 90              # canale aperto senza nome: riprova dopo 90 s
MAX_TRIES = 5             # poi smette fino alla prossima diretta


async def cancel_snapshot(peer_id=None) -> None:
    """Interrompe la lettura di un fotogramma in corso (esce da quella diretta) prima di aprire un canale:
    l'account sta in una diretta alla volta, e la lettura rallenterebbe quella da guardare."""
    snap = App.snapshot
    if snap and (peer_id is None or snap[0] == peer_id) and not snap[1].done():
        snap[1].cancel()
        try:
            await snap[1]
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


async def _frame_from_call(peer_id: int, tmp: Path) -> Path | None:
    """Entra un attimo nella diretta, scarica un pezzo da 1 s ed esce."""
    src = TelegramSource(App.live.reader(int(peer_id), 2))
    try:
        await asyncio.wait_for(src.open(), 40)
        chunk = None
        for _ in range(3):
            chunk = await asyncio.wait_for(src.next(), 45)
            if chunk:
                break
        if not chunk:
            return None
        f = tmp / f"chunk.{chunk.container or 'mp4'}"
        f.write_bytes(chunk.media)
        return f
    finally:
        try:
            await src.close()
        except Exception:  # noqa: BLE001
            pass


async def detect_channel(c: dict, fixtures: list[dict]) -> None:
    import scorebug
    import tempfile
    s = App.streamers.get(c["id"])
    tmp = Path(tempfile.mkdtemp(prefix="cs-snap-"))
    try:
        if s and s.state == "live":
            src = scorebug.latest_segment(s.dir)
            how = "video aperto"
        elif s and s.running:
            return                                   # si sta collegando: riprovo al prossimo giro
        elif config.DEMO:
            src, how = None, "demo"
        else:
            task = asyncio.create_task(_frame_from_call(c["peer_id"], tmp))
            App.snapshot = (c["peer_id"], task)
            try:
                src = await task
            finally:
                App.snapshot = None
            how = "pezzo di diretta"
        text = await scorebug.read_text(src) if src else ""
        if config.DEMO and not text:
            text = DemoSource.BUGS[c["id"] % len(DemoSource.BUGS)]
        res = scorebug.identify(text, fixtures)
        log.info("Canale %s (%s): letto «%s» → %s", c["id"], how, " ".join(text.split())[:80], res and res["title"])
        prev = App.detected.get(c["id"]) or {}
        App.detected[c["id"]] = {**(res or {"title": None}), "at": time.monotonic(), "tries": prev.get("tries", 0) + 1}
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.info("Canale %s: nome partita non letto (%s)", c["id"], e)
        prev = App.detected.get(c["id"]) or {"title": None}
        App.detected[c["id"]] = {**prev, "at": time.monotonic(), "tries": prev.get("tries", 0) + 1}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def scan_loop() -> None:
    """Dopo lo splash rilegge solo il canale che stai guardando, dal video già scaricato (nessun nuovo ingresso
    in altre dirette). Tutti gli altri canali si leggono solo durante lo splash (preload)."""
    import scorebug
    await asyncio.sleep(15)
    if not await scorebug.engine() and not config.DEMO:
        return
    while True:
        try:
            fixtures = None
            for c in load_channels():
                s = App.streamers.get(c["id"])
                if not (s and s.state == "live"):
                    continue
                d = App.detected.get(c["id"])
                # Una volta sola: trovato il nome non si rilegge più finché la diretta non finisce.
                # Se non si trova (pubblicità, replay, riquadro nascosto) riprova al massimo MAX_TRIES volte.
                if d and (d.get("title") or d.get("tries", 0) >= MAX_TRIES or time.monotonic() - d["at"] < RETRY_S):
                    continue
                if fixtures is None:
                    fixtures = (await today_matches()).get("matches") or []
                await detect_channel(c, fixtures)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Lettura nomi partite")
        await asyncio.sleep(20)


# --------------------------------------------------------------------------- preload durante lo splash

PRELOAD_BUDGET_S = 12      # massimo di tempo per leggere i nomi delle partite mentre gira lo splash
_preload = {"running": False, "done": 0, "total": 0, "step": ""}


async def _run_preload() -> None:
    try:
        channels = load_channels()
        now = time.monotonic()
        for c in channels:                   # spettatori aggiornati per i canali che contano
            cached = App.live_cache.get(c["peer_id"])
            if cached and now - cached[0] > 15 and (cached[1].get("viewers") or 0) >= MIN_VIEWERS:
                App.live_cache.pop(c["peer_id"], None)
        statuses = await asyncio.gather(*(live_status(c["peer_id"]) for c in channels))
        try:
            fixtures = (await asyncio.wait_for(today_matches(), 8)).get("matches") or []
        except Exception:  # noqa: BLE001
            fixtures = []
        for c, st in zip(channels, statuses):          # diretta finita: il nome si dimentica
            if st.get("live") is False:
                App.detected.pop(c["id"], None)
        # Solo i canali mai provati in questa diretta: un ingresso per canale, non a ogni apertura dell'app.
        todo = [(c, st) for c, st in zip(channels, statuses)
                if st.get("live") and (st.get("viewers") or 0) >= MIN_VIEWERS and c["id"] not in App.detected]
        todo.sort(key=lambda x: -(x[1].get("viewers") or 0))
        _preload["total"] = len(todo)
        import scorebug
        if not todo or (not config.DEMO and not await scorebug.engine()):
            return
        end = time.monotonic() + PRELOAD_BUDGET_S
        for i, (c, _st) in enumerate(todo):
            left = end - time.monotonic()
            if left < 2 or (not config.DEMO and any(o.running for o in App.streamers.values())):
                break                         # tempo finito, o qualcuno ha già aperto un canale
            _preload["step"] = f"Leggo le partite in diretta… {i + 1} di {len(todo)}"
            try:
                await asyncio.wait_for(detect_channel(c, fixtures), left)
            except asyncio.TimeoutError:
                log.info("Canale %s: nome partita non letto in tempo", c["id"])
            _preload["done"] = i + 1
    except Exception:  # noqa: BLE001
        log.exception("Preload")
    finally:
        _preload.update(running=False, step="")


async def preload(request: Request):
    """POST: avvia il caricamento di spettatori e nomi partita (lo fa lo splash). GET: a che punto è."""
    if not is_authed(request):
        return need_auth()
    if request.method == "POST" and not _preload["running"]:
        _preload.update(running=True, done=0, total=0, step="Controllo le dirette…")
        asyncio.create_task(_run_preload())
    return JSONResponse({k: _preload[k] for k in ("running", "done", "total", "step")})


# --------------------------------------------------------------------------- lifecycle

@asynccontextmanager
async def lifespan(_app):
    HLS_ROOT.mkdir(parents=True, exist_ok=True)
    if not config.APP_PASSWORD:
        log.info("Nessuna password: il sito si apre direttamente. Le modifiche ai canali si fanno solo da casa.")
    if config.DEMO:
        log.info("Modalità DEMO: canali di prova, nessun collegamento a Telegram.")
    else:
        await connect_telegram()
    App.last_activity = time.monotonic()
    scan = asyncio.create_task(scan_loop())
    rest = asyncio.create_task(rest_loop())
    yield
    scan.cancel()
    rest.cancel()
    for s in list(App.streamers.values()):
        await s.stop()
    await App.login.cancel()
    if App.client:
        await App.client.disconnect()


async def connect_telegram() -> None:
    from telethon import TelegramClient
    from telegram_live import TelegramLive

    if not config.TG_API_ID or not config.TG_API_HASH:
        App.tg_error = "Mancano TG_API_ID e TG_API_HASH nel file .env."
        log.error(App.tg_error)
        return
    client = TelegramClient(str(config.SESSION_PATH), config.TG_API_ID, config.TG_API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        App.tg_error = "Account Telegram non collegato: collegalo da Impostazioni → Account Telegram (da casa)."
        log.error(App.tg_error)
        await client.disconnect()
        return
    await client.get_dialogs()          # riempie la cache dei canali
    App.client, App.live = client, TelegramLive(client)
    App.tg_error = ""
    apply_events_bot()
    App.me = await client.get_me()
    log.info("Telegram collegato come %s", App.me.first_name)


def apply_events_bot() -> None:
    bot = events_bot()
    App.events_cache, App.events_error = None, ""
    if bot and App.client:
        from bot_events import BotEvents
        App.events = BotEvents(App.client, bot)
    else:
        App.events = None


# --------------------------------------------------------------------------- impostazioni e account

def account_info() -> dict:
    if config.DEMO:
        a = App.demo_account
        return {"connected": True, "name": a["name"], "username": a["username"], "phone": account.mask_phone(a["phone"]),
                "error": "", "step": App.login.step}
    me = App.me
    return {"connected": bool(App.client and me),
            "name": " ".join(x for x in (getattr(me, "first_name", ""), getattr(me, "last_name", "")) if x) if me else "",
            "username": getattr(me, "username", "") or "" if me else "",
            "phone": account.mask_phone(getattr(me, "phone", "")) if me else "",
            "error": App.tg_error, "step": App.login.step}


async def settings(request: Request):
    """GET: account e impostazioni. POST (solo da casa): {"events_bot": "nome" | ""}."""
    if not is_authed(request):
        return need_auth()
    if request.method == "POST":
        if not is_home(request):
            return need_home()
        body = await request.json()
        if "events_bot" in body:
            raw = str(body.get("events_bot") or "").strip()
            bot = account.clean_bot(raw) if raw else ""
            if bot is None:
                return JSONResponse({"error": "Nome del bot non valido (es. @NomeBot)."}, status_code=400)
            account.save_settings(events_bot=bot)
            apply_events_bot()
    return JSONResponse({"can_edit": is_home(request), "demo": config.DEMO, "account": account_info(),
                         "events_bot": events_bot(), "suggested_bot": account.SUGGESTED_BOT,
                         "lg_running": _lg["running"]})


async def account_login(request: Request):
    """Cambio account Telegram, solo da casa. action: send_code (phone), code (code), password (password), cancel."""
    if not is_authed(request):
        return need_auth()
    if not is_home(request):
        return need_home()
    body = await request.json()
    action = body.get("action")
    lg = App.login
    if action == "cancel":
        await lg.cancel()
        return JSONResponse({"ok": True, "account": account_info()})
    if action == "send_code":
        phone = account.clean_phone(str(body.get("phone") or ""))
        if not phone:
            return JSONResponse({"error": "Numero non valido. Scrivilo con il prefisso, es. +39 333 1234567."}, status_code=400)
        if config.DEMO:
            lg.phone, lg.step = phone, "code"
            return JSONResponse({"step": "code"})
        res = await lg.send_code(phone)
        return JSONResponse(res, status_code=400 if "error" in res else 200)
    if action not in ("code", "password"):
        return JSONResponse({"error": "Azione sconosciuta."}, status_code=400)
    if config.DEMO:
        if lg.step != action:
            return JSONResponse({"error": "Richiedi prima il codice."}, status_code=400)
        if action == "code" and str(body.get("code", "")).strip() == "22222":
            lg.step = "password"
            return JSONResponse({"step": "password"})
        App.demo_account = {"name": "Nuovo account demo", "username": "", "phone": lg.phone.lstrip("+")}
        lg.step = ""
        return JSONResponse({"ok": True, "account": account_info()})
    try:
        res = await (lg.sign_in_code(str(body.get("code") or "")) if action == "code"
                     else lg.sign_in_password(str(body.get("password") or "")))
    except Exception as e:  # noqa: BLE001
        log.exception("Accesso Telegram non riuscito")
        return JSONResponse({"error": f"Accesso non riuscito: {e}"}, status_code=502)
    if isinstance(res, dict):
        return JSONResponse(res, status_code=400 if "error" in res else 200)
    await switch_account()
    return JSONResponse({"ok": not App.tg_error, "error": App.tg_error or None, "account": account_info()})


async def switch_account() -> None:
    """Ferma le dirette, esce dal vecchio account e passa alla sessione appena creata."""
    for s in list(App.streamers.values()):
        await s.stop()
    App.streamers.clear()
    App.live_cache.clear()
    old = App.client
    App.client = App.live = App.events = App.me = None
    if old:
        try:
            await old.log_out()          # chiude anche la sessione del vecchio account su Telegram
        except Exception as e:  # noqa: BLE001
            log.warning("Uscita dal vecchio account non riuscita: %s", e)
            try:
                await old.disconnect()
            except Exception:  # noqa: BLE001
                pass
    await App.login.take_session()
    await connect_telegram()


RELEASE = "https://github.com/davidetrp/leonesportingclub/releases/download/tv-app/"


async def tv_apk(_: Request):
    """Indirizzo corto per Downloader sul Fire TV: http://SERVER/tv → APK dell'ultima versione."""
    return RedirectResponse(RELEASE + "canali-sport-firetv.apk", status_code=302)


async def tv_webos(_: Request):
    return RedirectResponse(RELEASE + "canali-sport-webos.ipk", status_code=302)


_lg = {"running": False, "steps": [], "result": None}


async def lg_install(request: Request):
    """Installa l'app sulla TV LG (POST con ip e passphrase) e ne restituisce l'avanzamento (GET)."""
    if not is_authed(request):
        return need_auth()
    if request.method == "POST" and not is_home(request):
        return need_home()
    if request.method == "POST" and not _lg["running"]:
        body = await request.json()
        ip = str(body.get("ip") or "").strip()
        passphrase = str(body.get("passphrase") or "").strip().upper()
        if not ip or not passphrase:
            return JSONResponse({"error": "Scrivi IP e passphrase della TV."}, status_code=400)
        _lg.update(running=True, steps=["Avvio…"], result=None)

        async def run():
            import tv_install
            try:
                _lg["result"] = await asyncio.to_thread(tv_install.install, ip, passphrase, _lg["steps"])
            except tv_install.InstallError as e:
                _lg["result"] = {"ok": False, "error": str(e)}
            except Exception as e:  # noqa: BLE001
                log.exception("Installazione TV LG non riuscita")
                _lg["result"] = {"ok": False, "error": f"Errore: {e}"}
            finally:
                _lg["running"] = False
        asyncio.create_task(run())
    return JSONResponse({"running": _lg["running"], "steps": _lg["steps"], "result": _lg["result"]})


async def guida(_: Request):
    return FileResponse(STATIC / "guida.html", headers={"Cache-Control": "no-cache"})


async def robots(_: Request):
    return FileResponse(STATIC / "robots.txt", media_type="text/plain")


routes = [
    Route("/", index),
    Route("/robots.txt", robots),
    Route("/guida", guida),
    Route("/tv", tv_apk),
    Route("/tv/webos", tv_webos),
    Route("/api/tv/lg", lg_install, methods=["GET", "POST"]),
    Route("/api/settings", settings, methods=["GET", "POST"]),
    Route("/api/preload", preload, methods=["GET", "POST"]),
    Route("/api/account", account_login, methods=["POST"]),
    Route("/api/login", login, methods=["POST"]),
    Route("/api/state", state),
    Route("/api/telegram-channels", telegram_channels),
    Route("/api/channels", add_channel, methods=["POST"]),
    Route("/api/channels/{cid:int}", edit_channel, methods=["PUT", "DELETE"]),
    Route("/api/play/{cid:int}", play, methods=["GET", "POST"]),
    Route("/api/stop/{cid:int}", stop, methods=["POST"]),
    Route("/api/matches", matches),
    Route("/api/events", events, methods=["GET", "POST"]),
    Route("/api/events/watch", watch_event, methods=["POST"]),
    Route("/api/events/unlock", unlock_events, methods=["GET", "POST"]),
    Route("/hls/{cid:int}/{file}", hls),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
]

# --------------------------------------------------------------------------- riposo quando nessuno usa l'app

REST_AFTER_S = 15 * 60


async def wake_up() -> None:
    async with App.wake_lock:
        if not App.resting:
            return
        log.info("Qualcuno ha aperto l'app: mi ricollego a Telegram")
        App.resting = False
        await connect_telegram()


async def rest_loop() -> None:
    """Dopo 15 minuti senza richieste dall'app si stacca da Telegram: il server resta acceso ma non fa niente."""
    while True:
        await asyncio.sleep(30)
        try:
            idle = time.monotonic() - App.last_activity
            busy = any(s.running for s in App.streamers.values()) or App.snapshot or App.login.step
            if config.DEMO or App.resting or not App.client or busy or idle < REST_AFTER_S:
                continue
            async with App.wake_lock:
                log.info("Nessuno usa l'app da %d minuti: riposo (staccato da Telegram)", idle // 60)
                client = App.client
                App.client = App.live = App.events = None
                App.live_cache.clear()
                App.events_cache = None
                App.resting = True
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Riposo")


class WakeMiddleware:
    """Ogni richiesta dell'app segna attività e, se il server riposa, lo risveglia prima di rispondere."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(("/api/", "/hls/")):
            App.last_activity = time.monotonic()
            if App.resting:
                await wake_up()
        await self.app(scope, receive, send)


app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(WakeMiddleware)])

if __name__ == "__main__":
    log.info("Apri http://<indirizzo-del-server>:%s dal telefono", config.PORT)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
