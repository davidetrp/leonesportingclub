"""Legge la lista «EVENTI IN PROGRAMMA» del bot (es. @CherryStreamingDbot) e la collega ai canali.

Il messaggio del bot ha questa forma:

    📌 EVENTI IN PROGRAMMA
    ⚽️ SERIE C
    Crotone - Casarano        ← testo con link
    (26/10) 20:30
    ...
    ⚠️ I link che trovi in questo messaggio sono personali...

Per ogni partita restituisce nome, campionato, orario e, se il link porta a un canale Telegram,
il canale (peer_id) e se sei già dentro.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    from telethon import errors, utils
    from telethon.tl import functions, types
except ImportError:  # modalità demo senza Telethon
    errors = utils = functions = types = None

log = logging.getLogger("bot_events")
TZ = ZoneInfo("Europe/Rome")

TIME_RE = re.compile(r"\(?\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s*\)?\s*[-–·]?\s*(\d{1,2})[:.](\d{2})")
INVITE_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(?:\+|joinchat/)([\w-]+)", re.I)
PRIVATE_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/c/(\d+)", re.I)
PUBLIC_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/([A-Za-z]\w{3,31})(?:[/?#].*)?$", re.I)
TG_JOIN_RE = re.compile(r"tg://join\?invite=([\w-]+)", re.I)
TG_RESOLVE_RE = re.compile(r"tg://resolve\?domain=([A-Za-z]\w{3,31})", re.I)
STOP_WORDS = ("⚠", "I link", "Telegram lagga", "Cerchi altro")
RESOLVE_TTL = 3600


def _clean_league(line: str) -> str:
    return re.sub(r"^[^\wÀ-ÿ]+", "", line).strip(" :*_") or line.strip()


def parse_message(text: str, links: list[tuple[str, str]], year: int) -> list[dict]:
    """links = [(testo del link, url)] nell'ordine del messaggio."""
    by_text = {}
    for t, u in links:
        by_text.setdefault(t.strip(), u)
    events: list[dict] = []
    league = ""
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(line.startswith(w) or w in line[:3] for w in STOP_WORDS):
            break
        if "EVENTI IN PROGRAMMA" in line.upper():
            continue
        hit = line if line in by_text else next((t for t in by_text if len(t) > 4 and t in line), None)
        if hit:
            current = {"name": hit, "league": league, "url": by_text[hit], "time": None, "start": None}
            events.append(current)
            continue
        m = TIME_RE.search(line)
        if m and len(line) < 30:
            if current is None or current["time"] is not None:
                continue
            day, month, yy, hh, mm = m.groups()
            y = int(yy) + (2000 if yy and len(yy) == 2 else 0) if yy else year
            try:
                start = datetime(y, int(month), int(day), int(hh), int(mm), tzinfo=TZ)
                current["time"] = f"{int(hh):02d}:{mm}"
                current["start"] = start.isoformat()
            except ValueError:
                pass
            continue
        league = _clean_league(line)
        current = None
    return events


def link_kind(url: str) -> tuple[str, str | None]:
    for rx, kind in ((INVITE_RE, "invite"), (TG_JOIN_RE, "invite"), (PRIVATE_RE, "private"),
                     (TG_RESOLVE_RE, "public"), (PUBLIC_RE, "public")):
        m = rx.search(url or "")
        if m:
            return kind, m.group(1)
    return "web", None


class BotEvents:
    def __init__(self, client, bot_username: str):
        self.client = client
        self.bot = bot_username.lstrip("@")
        self._resolved: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    async def _latest_list(self, bot):
        msgs = await self.client.get_messages(bot, limit=20)
        for m in msgs:
            if not m.out and m.message and "EVENTI" in m.message.upper():
                return m
        return None

    async def fetch(self, refresh: bool = False) -> list[dict]:
        async with self._lock:
            bot = await self.client.get_input_entity(self.bot)
            msg = await self._latest_list(bot)
            if msg is None:
                log.info("Nessuna lista eventi: mando /start a @%s", self.bot)
                await self.client.send_message(bot, "/start")
                await asyncio.sleep(3)
                msg = await self._latest_list(bot)
            elif refresh:
                try:
                    await msg.click(text=lambda t: "AGGIORNA" in t.upper())
                    await asyncio.sleep(2.5)
                    msg = await self._latest_list(bot) or msg
                except Exception as e:  # noqa: BLE001
                    log.info("Aggiorna non riuscito: %s", e)
            if msg is None:
                return []
            links = []
            for ent, txt in msg.get_entities_text():
                if isinstance(ent, types.MessageEntityTextUrl):
                    links.append((txt, ent.url))
                elif isinstance(ent, types.MessageEntityUrl):
                    links.append((txt, txt))
            events = parse_message(msg.message, links, datetime.now(TZ).year)
            for ev in events:
                ev.update(await self._resolve(ev["url"]))
                ev.pop("url_raw", None)
            kinds = {}
            for ev in events:
                kinds[ev["kind"]] = kinds.get(ev["kind"], 0) + 1
            log.info("Eventi dal bot: %d (%s)", len(events), kinds)
            return events

    async def _resolve(self, url: str) -> dict:
        kind, key = link_kind(url)
        info = {"kind": kind, "peer_id": None, "member": False, "channel_title": None}
        if kind == "web" or key is None:
            return info
        cached = self._resolved.get(url)
        if cached and time.monotonic() - cached[0] < RESOLVE_TTL:
            return cached[1]
        try:
            if kind == "invite":
                res = await self.client(functions.messages.CheckChatInviteRequest(key))
                if isinstance(res, types.ChatInviteAlready):
                    info.update(peer_id=utils.get_peer_id(res.chat), member=True, channel_title=res.chat.title)
                elif isinstance(res, types.ChatInvitePeek):
                    info.update(peer_id=utils.get_peer_id(res.chat), channel_title=res.chat.title)
                else:
                    info.update(channel_title=getattr(res, "title", None))
            elif kind == "private":
                info.update(peer_id=int(f"-100{key}"))
                try:
                    ent = await self.client.get_entity(info["peer_id"])
                    info.update(member=not getattr(ent, "left", True), channel_title=getattr(ent, "title", None))
                except (ValueError, errors.RPCError):
                    pass
            elif kind == "public":
                ent = await self.client.get_entity(key)
                if isinstance(ent, types.Channel):
                    info.update(peer_id=utils.get_peer_id(ent), member=not ent.left, channel_title=ent.title)
                else:
                    info["kind"] = "web"
        except errors.FloodWaitError as e:
            log.info("Pausa Telegram di %ss leggendo un link", e.seconds)
            return info
        except Exception as e:  # noqa: BLE001
            log.info("Link non risolto (%s): %s", kind, e)
            return info
        self._resolved[url] = (time.monotonic(), info)
        return info

    async def join(self, url: str) -> int | None:
        """Entra nel canale del link (solo su richiesta esplicita dall'app) e ne restituisce il peer_id."""
        kind, key = link_kind(url)
        if kind == "invite":
            try:
                upd = await self.client(functions.messages.ImportChatInviteRequest(key))
                chat = upd.chats[0]
            except errors.UserAlreadyParticipantError:
                res = await self.client(functions.messages.CheckChatInviteRequest(key))
                chat = res.chat
        elif kind == "public":
            ent = await self.client.get_entity(key)
            await self.client(functions.channels.JoinChannelRequest(ent))
            chat = ent
        elif kind == "private":
            return int(f"-100{key}")
        else:
            return None
        self._resolved.pop(url, None)
        await self.client.get_dialogs(limit=30)
        return utils.get_peer_id(chat)


def demo_events() -> list[dict]:
    today = datetime.now(TZ)
    def at(h, m):
        return today.replace(hour=h, minute=m, second=0, microsecond=0).isoformat()
    return [
        {"name": "Crotone - Casarano", "league": "SERIE C", "time": "20:30", "start": at(20, 30),
         "url": "https://t.me/+demo1", "kind": "invite", "peer_id": -1, "member": True, "channel_title": "Demo 1"},
        {"name": "Picerno - Salernitana", "league": "SERIE C", "time": "20:30", "start": at(20, 30),
         "url": "https://t.me/+demo2", "kind": "invite", "peer_id": -2, "member": True, "channel_title": "Demo 2"},
        {"name": "Siviglia - Osasuna", "league": "LALIGA", "time": "21:00", "start": at(21, 0),
         "url": "https://t.me/+demo7", "kind": "invite", "peer_id": -7, "member": False, "channel_title": "Demo 7"},
    ]
