"""Server di Canali Sport: app web + API + stream HLS delle dirette Telegram.

Avvio:  python server.py      (prima volta: python login.py)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import config
from streamer import DemoSource, SlotStreamer, TelegramSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

SLOTS = 5
STATIC = config.BASE_DIR / "static"
HLS_ROOT = config.DATA_DIR / "hls"
SLOTS_FILE = config.DATA_DIR / "canali.json"
SECRET_FILE = config.DATA_DIR / "secret.key"
LIVE_CACHE_S = 20


class App:
    client = None          # TelegramClient
    live = None            # TelegramLive
    tg_error = ""
    streamers: dict[int, SlotStreamer] = {}
    live_cache: dict[int, tuple[float, dict]] = {}


# --------------------------------------------------------------------------- persistence

def load_slots() -> list[dict]:
    slots = [{"name": "", "peer_id": None} for _ in range(SLOTS)]
    if config.DEMO:
        slots = [{"name": f"Demo {i + 1}", "peer_id": -(i + 1)} for i in range(SLOTS)]
    try:
        saved = json.loads(SLOTS_FILE.read_text())
        for i, s in enumerate(saved[:SLOTS]):
            slots[i] = {"name": str(s.get("name") or ""), "peer_id": s.get("peer_id")}
    except (OSError, ValueError):
        pass
    return slots


def save_slots(slots: list[dict]) -> None:
    tmp = SLOTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(slots, ensure_ascii=False, indent=2))
    tmp.replace(SLOTS_FILE)


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

def make_source(slot: int, tmp: Path):
    if config.DEMO:
        return DemoSource(slot, tmp)
    peer_id = load_slots()[slot - 1]["peer_id"]
    return TelegramSource(App.live.reader(int(peer_id), config.STREAM_QUALITY))


def streamer(slot: int) -> SlotStreamer:
    if slot not in App.streamers:
        App.streamers[slot] = SlotStreamer(slot, HLS_ROOT, make_source)
    return App.streamers[slot]


async def live_status(peer_id) -> dict:
    if peer_id is None:
        return {"live": None}
    if config.DEMO:
        return {"live": True, "rtmp": True, "title": "Segnale di prova", "viewers": 1}
    if not App.live:
        return {"live": None}
    now = time.monotonic()
    cached = App.live_cache.get(peer_id)
    if cached and now - cached[0] < LIVE_CACHE_S:
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
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


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
    slots = load_slots()
    statuses = await asyncio.gather(*(live_status(s["peer_id"]) for s in slots))
    out = []
    for i, (s, st) in enumerate(zip(slots, statuses), start=1):
        stv = App.streamers.get(i)
        out.append({"slot": i, **s, **st,
                    "stream": stv.state if stv else "idle",
                    "message": stv.message if stv else ""})
    return JSONResponse({"demo": config.DEMO, "telegram_error": App.tg_error, "slots": out})


async def telegram_channels(request: Request):
    if not is_authed(request):
        return need_auth()
    if config.DEMO:
        return JSONResponse({"channels": [{"peer_id": -(i + 1), "title": f"Demo {i + 1}", "kind": "demo"}
                                          for i in range(SLOTS)]})
    if not App.live:
        return JSONResponse({"error": App.tg_error or "Telegram non collegato."}, status_code=503)
    return JSONResponse({"channels": await App.live.list_channels()})


async def update_slot(request: Request):
    if not is_authed(request):
        return need_auth()
    n = int(request.path_params["n"])
    if not 1 <= n <= SLOTS:
        return JSONResponse({"error": "Canale inesistente."}, status_code=404)
    body = await request.json()
    slots = load_slots()
    peer = body.get("peer_id")
    slots[n - 1] = {"name": str(body.get("name") or "")[:40], "peer_id": int(peer) if peer not in (None, "") else None}
    save_slots(slots)
    if n in App.streamers:
        await App.streamers[n].stop()
    return JSONResponse({"ok": True})


async def play(request: Request):
    if not is_authed(request):
        return need_auth()
    n = int(request.path_params["n"])
    if not 1 <= n <= SLOTS or load_slots()[n - 1]["peer_id"] is None:
        return JSONResponse({"stream": "idle", "message": "Canale non collegato."}, status_code=404)
    if not config.DEMO and not App.live:
        return JSONResponse({"stream": "error", "message": App.tg_error}, status_code=503)
    s = streamer(n)
    if request.method == "POST" and s.state in ("idle", "offline", "error", "unsupported"):
        App.live_cache.pop(load_slots()[n - 1]["peer_id"], None)
        s.start()
    s.touch()
    return JSONResponse({"stream": s.state, "message": s.message})


async def stop(request: Request):
    if not is_authed(request):
        return need_auth()
    n = int(request.path_params["n"])
    if n in App.streamers:
        await App.streamers[n].stop()
    return JSONResponse({"ok": True})


async def hls(request: Request):
    if not is_authed(request):
        return Response(status_code=401)
    n = int(request.path_params["n"])
    name = request.path_params["file"]
    if n not in App.streamers or "/" in name or ".." in name:
        return Response(status_code=404)
    s = App.streamers[n]
    s.touch()
    path = s.dir / name
    if not path.exists():
        return Response(status_code=404)
    if name.endswith(".m3u8"):
        return Response(path.read_bytes(), media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache, no-store"})
    return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "max-age=60"})


# --------------------------------------------------------------------------- lifecycle

@asynccontextmanager
async def lifespan(_app):
    HLS_ROOT.mkdir(parents=True, exist_ok=True)
    if not config.APP_PASSWORD:
        if config.PUBLIC:
            raise SystemExit("APP_PASSWORD è obbligatoria quando il sito è online. Impostala in .env.")
        log.warning("APP_PASSWORD vuota: chiunque raggiunga il server può usarlo. Impostala in .env!")
    if config.DEMO:
        log.info("Modalità DEMO: canali di prova, nessun collegamento a Telegram.")
    else:
        await connect_telegram()
    yield
    for s in list(App.streamers.values()):
        await s.stop()
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
        App.tg_error = "Account Telegram non collegato: esegui  python login.py  sul server."
        log.error(App.tg_error)
        await client.disconnect()
        return
    await client.get_dialogs()          # riempie la cache dei canali
    App.client, App.live = client, TelegramLive(client)
    me = await client.get_me()
    log.info("Telegram collegato come %s", me.first_name)


async def robots(_: Request):
    return FileResponse(STATIC / "robots.txt", media_type="text/plain")


routes = [
    Route("/", index),
    Route("/robots.txt", robots),
    Route("/api/login", login, methods=["POST"]),
    Route("/api/state", state),
    Route("/api/telegram-channels", telegram_channels),
    Route("/api/slots/{n:int}", update_slot, methods=["PUT"]),
    Route("/api/play/{n:int}", play, methods=["GET", "POST"]),
    Route("/api/stop/{n:int}", stop, methods=["POST"]),
    Route("/hls/{n:int}/{file}", hls),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    log.info("Apri http://<indirizzo-del-server>:%s dal telefono", config.PORT)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
