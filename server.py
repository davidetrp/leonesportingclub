"""Server di Canali Sport: app web + API + stream HLS delle dirette Telegram.

Avvio:  python avvia.py   (l'account Telegram si collega da Impostazioni nell'app, oppure con python login.py)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
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
# Un solo canale alla volta: Telegram tiene l'account in una diretta alla volta (vedi play()).
STALE_S = 8              # un canale non chiesto da 8 s è stato lasciato (chi guarda lo chiede ogni 2–5 s)
STATIC = config.BASE_DIR / "static"
HLS_ROOT = config.DATA_DIR / "hls"
SLOTS_FILE = config.DATA_DIR / "canali.json"
SECRET_FILE = config.DATA_DIR / "secret.key"
LIVE_CACHE_S = 45
MIN_VIEWERS = 50          # sotto questa soglia il canale è nascosto e non si carica niente
SMALL_CACHE_S = 300
LIVE_INFO_SEM = asyncio.Semaphore(3)


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
    detected: dict[int, dict] = {}     # ultimo tentativo di lettura: {cid: {at}}
    names: dict[int, dict] = {}        # memoria dei nomi imparati (data/nomi-partite.json)
    offline_since: dict[int, float] = {}
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
        async with LIVE_INFO_SEM:            # al massimo 3 richieste insieme: niente raffiche verso Telegram
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
        note_live(c["id"], st.get("live"))
        det = remembered(c["id"]) if st.get("live") else None
        out.append({**c, **st,
                    "match": det,
                    "thumb": int(_thumb_at.get(c["id"], 0)) or None,
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
        # Telegram tiene l'account in UNA diretta alla volta: entrare in un'altra butta fuori quella in corso
        # (e le due si rubano il posto a vicenda, con BAD_REQUEST continui). Quindi un solo canale aperto:
        # chi apre un canale nuovo lo prende, gli altri dispositivi vedono un avviso. Lo stesso canale
        # su più dispositivi va benissimo (un solo download).
        for o_id, o in list(App.streamers.items()):
            if o_id != cid and o.running:
                busy = time.monotonic() - o.last_access <= STALE_S
                log.info("Chiudo il canale %s per aprire il %s%s", o.slot, cid, " (lo guardava un altro dispositivo)" if busy else "")
                await o.stop("replaced", f"Su un altro dispositivo è stato aperto «{ch['name']}». Telegram permette "
                             "una diretta alla volta per account.") if busy else await o.stop()
    if request.method == "POST" and s.state in ("idle", "offline", "error", "unsupported", "replaced"):
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
    # Mentre si guarda una diretta la lista del bot si aggiorna di rado: meno richieste all'account Telegram.
    ttl = EVENTS_CACHE_S * 7 if any(st.running for st in App.streamers.values()) else EVENTS_CACHE_S
    if not refresh and App.events_cache and now - App.events_cache[0] < ttl:
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
    reader = App.live.reader(int(peer_id), 1)        # qualità media: il riquadro si legge e il pezzo arriva prima
    reader.quick = True
    src = TelegramSource(reader)
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


# Memoria dei nomi: imparato il nome in un fotogramma, resta anche durante pubblicità, intervallo e replay
# (quando il riquadro non c'è) e anche dopo un riavvio del server. Si dimentica quando la partita è finita,
# quando il canale non trasmette più da 20 minuti, o quando nel video si legge un'altra partita.
NAMES_FILE = config.DATA_DIR / "nomi-partite.json"
NAME_MAX_S = 2 * 3600 + 45 * 60          # durata massima di un nome imparato senza orario della partita
OFFLINE_FORGET_S = 20 * 60
ATTEMPT_EVERY_S = 45                     # canale aperto senza nome: un tentativo ogni 45 s (solo dal video già scaricato)


def _load_names() -> dict:
    try:
        data = json.loads(NAMES_FILE.read_text())
        return {int(k): v for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_names() -> None:
    try:
        tmp = NAMES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({str(k): v for k, v in App.names.items()}, ensure_ascii=False))
        os.replace(tmp, NAMES_FILE)
    except OSError as e:
        log.info("Nomi partite non salvati: %s", e)


def _cached_fixtures() -> list[dict]:
    """Partite di oggi già in memoria (senza scaricarle di nuovo)."""
    import matches as mt
    best = None
    for _k, (_t, res) in getattr(mt, "_cache", {}).items():
        best = res
    return (best or {}).get("matches") or []


MULTI_WINDOW_S = 30 * 60    # due partite diverse lette entro mezz'ora (e giocate insieme) = Diretta Gol
RECHECK_S = 150             # canale aperto con il nome già noto: riletto ogni 2,5 minuti (video già scaricato)
MULTI_WORDS = re.compile(r"DIRETTA\s*GOL|DIRETTA\s*GOAL|GOL\s*SHOW|GOAL\s*SHOW|MULTI\s*PARTITA|MULTIPARTITA|TUTTI\s+I\s+GOL|CONFERENCE\s*CALL")


def _fixture(fid) -> dict | None:
    return next((m for m in _cached_fixtures() if fid and m.get("id") == fid), None)


def _fixture_expired(fx: dict, learned: float, now: float) -> bool:
    try:
        from datetime import datetime
        if now > datetime.fromisoformat(fx["start"]).timestamp() + NAME_MAX_S:
            return True
    except (KeyError, ValueError):
        pass
    return fx.get("state") == "post" and now - learned > 15 * 60


def remembered(cid: int) -> dict | None:
    """Nome imparato per il canale, se è ancora valido; con il punteggio aggiornato dai risultati in diretta.
    Per una Diretta Gol: il titolo «Diretta Gol» e le partite viste finora."""
    n = App.names.get(cid)
    if not n:
        return None
    now = time.time()
    learned = n.get("learned", now)
    off = App.offline_since.get(cid)
    if n.get("multi"):
        games = n.get("games") or []
        fxs = [f for f in (_fixture(g.get("fid")) for g in games) if f]
        expired = now - learned > NAME_MAX_S or (fxs and all(_fixture_expired(f, learned, now) for f in fxs))
        if expired or (off and now - off > OFFLINE_FORGET_S):
            App.names.pop(cid, None)
            _save_names()
            return None
        return {"title": "Diretta Gol", "score": None, "league": n.get("league"), "multi": True,
                "games": [g["title"] for g in games][:8], "details": False}
    fx = _fixture(n.get("fixture_id"))
    expired = now - learned > NAME_MAX_S
    if fx:
        expired = _fixture_expired(fx, learned, now)
    if expired or (off and now - off > OFFLINE_FORGET_S):
        App.names.pop(cid, None)
        _save_names()
        return None
    score = n.get("score")
    if fx and fx.get("home_score") not in (None, "") and fx.get("away_score") not in (None, ""):
        score = f"{fx['home_score']}-{fx['away_score']}"
    return {"title": n["title"], "score": score, "league": n.get("league"),
            "details": bool(config.DEMO or (fx and fx.get("code") and fx.get("id")))}


def _concurrent(fid_a, fid_b) -> bool:
    """Le due partite si giocano (o si sono giocate) insieme? Senza dati, si suppone di sì."""
    fa, fb = _fixture(fid_a), _fixture(fid_b)
    if not fa or not fb:
        return True
    if "post" in (fa.get("state"), fb.get("state")) and "in" in (fa.get("state"), fb.get("state")):
        return False                           # una è finita e l'altra è in corso: il canale è passato all'altra
    try:
        from datetime import datetime
        return abs(datetime.fromisoformat(fa["start"]).timestamp() - datetime.fromisoformat(fb["start"]).timestamp()) < 90 * 60
    except (KeyError, ValueError):
        return True


def learn(cid: int, res: dict | None, text: str = "") -> None:
    """Aggiorna la memoria del canale con una lettura. Riconosce la Diretta Gol: più partite diverse,
    giocate insieme, viste sullo stesso canale a breve distanza (o la scritta «Diretta Gol» nel riquadro)."""
    now = time.time()
    old = App.names.get(cid)
    flat = " ".join((text or "").upper().split())
    says_multi = bool(MULTI_WORDS.search(flat))
    if not res and not says_multi:
        return
    if old and old.get("multi"):
        if res and res.get("matched") and all(g.get("fid") != res.get("fixture_id") for g in old.get("games", [])):
            old["games"].append({"fid": res.get("fixture_id"), "title": res["title"]})
            log.info("Canale %s (Diretta Gol): vista anche «%s»", cid, res["title"])
            _save_names()
        return
    if says_multi or (old and res and res.get("matched") and old.get("fixture_id")
                      and res.get("fixture_id") != old["fixture_id"]
                      and now - old.get("seen", old.get("learned", now)) < MULTI_WINDOW_S
                      and _concurrent(old["fixture_id"], res.get("fixture_id"))):
        games = []
        if old and old.get("fixture_id"):
            games.append({"fid": old["fixture_id"], "title": old["title"]})
        if res and res.get("matched") and all(g["fid"] != res.get("fixture_id") for g in games):
            games.append({"fid": res.get("fixture_id"), "title": res["title"]})
        App.names[cid] = {"multi": True, "games": games, "league": (res or old or {}).get("league"),
                          "learned": now, "seen": now}
        log.info("Canale %s: è una Diretta Gol (%s)", cid, ", ".join(g["title"] for g in games) or "scritta nel riquadro")
        _save_names()
        return
    if old and old.get("title") == res["title"]:
        old["seen"] = now
        if res.get("score"):
            old["score"] = res["score"]
        return
    if old and old.get("fixture_id") and not res.get("matched"):
        return                                # un nome «vero» non si sostituisce con sigle lette a caso
    App.names[cid] = {"title": res["title"], "score": res.get("score"), "league": res.get("league"),
                      "fixture_id": res.get("fixture_id"), "learned": now, "seen": now}
    log.info("Canale %s: imparato «%s»", cid, res["title"])
    _save_names()


def note_live(cid: int, live) -> None:
    if live is False:
        App.offline_since.setdefault(cid, time.time())
    elif live:
        App.offline_since.pop(cid, None)


THUMBS = config.DATA_DIR / "anteprime"
THUMB_EVERY_S = 120          # canale aperto: anteprima aggiornata ogni 2 minuti (dal video già scaricato)
_thumb_at: dict[int, float] = {}


async def make_thumb(src: Path, cid: int) -> None:
    """Piccola immagine del video (480 px) per la scheda del canale e per lo schermo di caricamento."""
    THUMBS.mkdir(exist_ok=True)
    out = THUMBS / f"{cid}.jpg"
    tmp = THUMBS / f"{cid}.tmp.jpg"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-frames:v", "1",
        "-vf", "scale=480:-2", "-q:v", "7", str(tmp),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), 15)
    except asyncio.TimeoutError:
        proc.kill()
        return
    if proc.returncode == 0 and tmp.exists():
        os.replace(tmp, out)
        _thumb_at[cid] = time.time()


async def thumb(request: Request):
    if not is_authed(request):
        return need_auth()
    f = THUMBS / f"{int(request.path_params['cid'])}.jpg"
    if not f.exists():
        return Response(status_code=404)
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=60"})


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
            if _someone_watching():
                return                               # una diretta si è appena aperta: non entro in un'altra
            # Nessuna attesa tra il controllo qui sopra e la registrazione del fotogramma in corso: così se
            # in questo istante apri un canale, la lettura viene interrotta prima di disturbarlo.
            task = asyncio.create_task(_frame_from_call(c["peer_id"], tmp))
            App.snapshot = (c["peer_id"], task)
            try:
                src = await task
            finally:
                App.snapshot = None
            how = "pezzo di diretta"
        if src:
            await make_thumb(src, c["id"])
        text = await scorebug.read_text(src) if src else ""
        if config.DEMO and not text:
            text = DemoSource.BUGS[c["id"] % len(DemoSource.BUGS)]
        res = scorebug.identify(text, fixtures)
        log.info("Canale %s (%s): letto «%s» → %s", c["id"], how, " ".join(text.split())[:80], res and res["title"])
        learn(c["id"], res, text)
        App.detected[c["id"]] = {"at": time.monotonic()}
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.info("Canale %s: nome partita non letto (%s)", c["id"], e)
        App.detected[c["id"]] = {"at": time.monotonic()}
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
                # Nome già imparato: non si legge più. Altrimenti un tentativo ogni 45 s dal video già scaricato
                # (niente traffico verso Telegram), finché non compare il riquadro del punteggio.
                if time.time() - _thumb_at.get(c["id"], 0) > THUMB_EVERY_S:
                    seg = __import__("scorebug").latest_segment(s.dir)
                    if seg:
                        await make_thumb(seg, c["id"])
                # Senza nome: un tentativo ogni 45 s. Con il nome: una rilettura ogni 2,5 minuti, per accorgersi
                # di una Diretta Gol o di un cambio di partita. Sempre dal video già scaricato (niente Telegram).
                d = App.detected.get(c["id"])
                wait = RECHECK_S if remembered(c["id"]) else ATTEMPT_EVERY_S
                if d and time.monotonic() - d["at"] < wait:
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

_preload = {"running": False, "done": 0, "total": 0, "step": ""}


NEXT_MAX_S = 20             # tempo massimo per leggere il nome di un canale


def _someone_watching() -> bool:
    return not config.DEMO and any(o.running for o in App.streamers.values())


async def _run_preload() -> None:
    """Legge i nomi delle partite di tutti i canali in diretta, uno alla volta, dal più seguito.
    L'app intanto mostra già la lista; finita la lettura apre da sola l'ultimo canale guardato.
    Si ferma appena qualcuno apre una diretta (Telegram tiene l'account in una diretta alla volta)."""
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
        for c, st in zip(channels, statuses):
            note_live(c["id"], st.get("live"))
        recent = lambda cid: App.detected.get(cid) and time.monotonic() - App.detected[cid]["at"] < 600  # noqa: E731
        todo = [c for c, st in sorted(zip(channels, statuses), key=lambda x: -(x[1].get("viewers") or 0))
                if st.get("live") and (st.get("viewers") or 0) >= MIN_VIEWERS
                and not remembered(c["id"]) and not recent(c["id"])]
        _preload.update(total=len(todo), pending=[c["id"] for c in todo])
        import scorebug
        if not todo or (not config.DEMO and not await scorebug.engine()):
            return
        for i, c in enumerate(todo):
            if _someone_watching():
                break
            _preload["step"] = f"Leggo le partite in diretta… {i + 1} di {len(todo)}"
            try:
                await asyncio.wait_for(detect_channel(c, fixtures), NEXT_MAX_S)
            except asyncio.TimeoutError:
                log.info("Canale %s: nome partita non letto in tempo", c["id"])
            _preload["done"] = i + 1
            _preload["pending"] = [x["id"] for x in todo[i + 1:]]
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("Preload")
    finally:
        _preload.update(running=False, step="", pending=[])


async def preload(request: Request):
    """POST: avvia il caricamento di spettatori e nomi partita (lo fa lo splash). GET: a che punto è."""
    if not is_authed(request):
        return need_auth()
    if request.method == "POST" and not _preload["running"]:
        _preload.update(running=True, done=0, total=0, pending=[], step="Controllo le dirette…")
        asyncio.create_task(_run_preload())
    return JSONResponse({k: _preload.get(k) for k in ("running", "done", "total", "step", "pending")})


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
    App.names = _load_names()
    if THUMBS.exists():                       # anteprime di prima del riavvio
        for f in THUMBS.glob("*.jpg"):
            if f.stem.isdigit() and time.time() - f.stat().st_mtime < 3 * 3600:
                _thumb_at[int(f.stem)] = f.stat().st_mtime
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


HLSJS_FILE = config.DATA_DIR / "hls-1.5.15.min.js"
HLSJS_URLS = ["https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js",
              "https://unpkg.com/hls.js@1.5.15/dist/hls.min.js"]


async def hlsjs(_: Request):
    """Il lettore video (hls.js) servito dal Mac: lo scarica una volta e lo tiene in data/,
    così l'avvio della diretta non dipende ogni volta da un sito esterno."""
    if not HLSJS_FILE.exists():
        def download():
            import urllib.request
            from matches import SSL_CTX
            for url in HLSJS_URLS:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "LeoneSportingClub"})
                    with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
                        body = r.read()
                    if len(body) > 100_000 and b"Hls" in body:
                        tmp = HLSJS_FILE.with_suffix(".tmp")
                        tmp.write_bytes(body)
                        os.replace(tmp, HLSJS_FILE)
                        return True
                except Exception as e:  # noqa: BLE001
                    log.info("hls.js non scaricato da %s: %s", url, e)
            return False
        if not await asyncio.to_thread(download):
            return RedirectResponse(HLSJS_URLS[0], status_code=302)
    return FileResponse(HLSJS_FILE, media_type="application/javascript",
                        headers={"Cache-Control": "public, max-age=86400"})


async def match_info(request: Request):
    """Formazioni, eventi e statistiche della partita riconosciuta sul canale (da ESPN, mai da Telegram)."""
    if not is_authed(request):
        return need_auth()
    cid = int(request.path_params["cid"])
    if config.DEMO:
        from matches import demo_details
        return JSONResponse(demo_details())
    n = App.names.get(cid) or {}
    fx_list = _cached_fixtures() or (await today_matches()).get("matches") or []
    fx = next((m for m in fx_list if n.get("fixture_id") and m.get("id") == n["fixture_id"]), None)
    if not fx or not fx.get("code"):
        return JSONResponse({"error": "Per questo canale non ho riconosciuto una partita con i dettagli disponibili."},
                            status_code=404)
    from matches import match_details
    try:
        data = await asyncio.wait_for(match_details(fx["code"], fx["id"]), 15)
    except Exception as e:  # noqa: BLE001
        log.info("Dettagli partita non disponibili: %s", e)
        return JSONResponse({"error": "Dettagli della partita non disponibili in questo momento."}, status_code=502)
    return JSONResponse({**data, "league": fx.get("league"), "start": fx.get("start")}, headers={"Cache-Control": "no-cache"})


async def health(_: Request):
    """Per il programma di avvio: c'è una diretta in corso? (allora non si riavvia per aggiornare)"""
    return JSONResponse({"streaming": sum(1 for s in App.streamers.values() if s.running),
                         "resting": App.resting})


routes = [
    Route("/", index),
    Route("/robots.txt", robots),
    Route("/guida", guida),
    Route("/tv", tv_apk),
    Route("/tv/webos", tv_webos),
    Route("/api/tv/lg", lg_install, methods=["GET", "POST"]),
    Route("/api/settings", settings, methods=["GET", "POST"]),
    Route("/api/preload", preload, methods=["GET", "POST"]),
    Route("/api/health", health),
    Route("/api/match/{cid:int}", match_info),
    Route("/api/thumb/{cid:int}", thumb),
    Route("/vendor/hls.min.js", hlsjs),
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
        if scope["type"] == "http" and scope["path"].startswith(("/api/", "/hls/")) and scope["path"] != "/api/health":
            App.last_activity = time.monotonic()
            if App.resting:
                await wake_up()
        await self.app(scope, receive, send)


app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(WakeMiddleware)])

if __name__ == "__main__":
    log.info("Apri http://<indirizzo-del-server>:%s dal telefono", config.PORT)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
