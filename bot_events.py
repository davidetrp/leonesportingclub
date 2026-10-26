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


GATE_WORDS = ("sblocc", "unisc", "iscriv", "verific", "requisit", "per accedere", "entra nei", "entra nel")
VERIFY_WORDS = ("sblocc", "verific", "fatto", "controlla", "continua", "conferma", "accedi", "unlock", "check", "done", "✅")
MAX_JOINS = 12


def _norm_url(url: str) -> str:
    return (url or "").strip()


class BotEvents:
    def __init__(self, client, bot_username: str):
        self.client = client
        self.bot = bot_username.lstrip("@")
        self._resolved: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()
        self.locked = False
        self.gate_text = ""

    # ---------------------------------------------------------------- lettura
    @staticmethod
    def _is_list(m) -> bool:
        return bool(m and m.message and "EVENTI IN PROGRAMMA" in m.message.upper())

    @staticmethod
    def _is_gate(m) -> bool:
        if not m:
            return False
        # Serve una parola da «richiesta di sblocco» nel testo: i messaggi pubblicitari del bot
        # (es. «CLICCA QUI PER IL CANALE») non vanno scambiati per requisiti.
        text = (m.message or "").lower()
        return any(w in text for w in GATE_WORDS)

    async def _state(self, bot):
        """(ultimo messaggio con la lista, ultimo messaggio del bot se è una richiesta di sblocco)."""
        msgs = await self.client.get_messages(bot, limit=20)
        incoming = [m for m in msgs if not m.out]
        list_msg = next((m for m in incoming if self._is_list(m)), None)
        gate = None
        for m in incoming:                 # dal più recente fino alla lista
            if m is list_msg:
                break
            if self._is_gate(m):
                gate = m
                break
        return list_msg, gate

    def _set_gate(self, gate) -> None:
        self.locked = gate is not None
        self.gate_text = (gate.message or "")[:300] if gate is not None else ""

    async def fetch(self, refresh: bool = False) -> list[dict]:
        async with self._lock:
            bot = await self.client.get_input_entity(self.bot)
            msg, gate = await self._state(bot)
            if msg is None and gate is None:
                log.info("Nessuna lista eventi: mando /start a @%s", self.bot)
                await self.client.send_message(bot, "/start")
                await asyncio.sleep(3)
                msg, gate = await self._state(bot)
            elif refresh and msg is not None and gate is None:
                try:
                    await msg.click(text=lambda t: "AGGIORNA" in t.upper())
                    await asyncio.sleep(2.5)
                    msg, gate = await self._state(bot)
                except Exception as e:  # noqa: BLE001
                    log.info("Aggiorna non riuscito: %s", e)
            self._set_gate(gate)
            if gate is not None:
                log.info("Il bot chiede di sbloccare la lista")
            if msg is None:
                return []
            return await self._events_from(msg)

    async def _events_from(self, msg) -> list[dict]:
        links = []
        for ent, txt in msg.get_entities_text():
            if isinstance(ent, types.MessageEntityTextUrl):
                links.append((txt, ent.url))
            elif isinstance(ent, types.MessageEntityUrl):
                links.append((txt, txt))
        events = parse_message(msg.message, links, datetime.now(TZ).year)
        for ev in events:
            ev.update(await self._resolve(ev["url"]))
        kinds = {}
        for ev in events:
            kinds[ev["kind"]] = kinds.get(ev["kind"], 0) + 1
        log.info("Eventi dal bot: %d (%s)", len(events), kinds)
        return events

    # ---------------------------------------------------------------- sblocco
    def _gate_links(self, gate) -> tuple[list[str], list]:
        urls, verify = [], []
        for row in gate.buttons or []:
            for b in row:
                if b.url:
                    urls.append(_norm_url(b.url))
                elif b.data is not None and any(w in (b.text or "").lower() for w in VERIFY_WORDS):
                    verify.append(b)
        for ent, txt in gate.get_entities_text():
            if isinstance(ent, types.MessageEntityTextUrl):
                urls.append(_norm_url(ent.url))
            elif isinstance(ent, types.MessageEntityUrl):
                urls.append(_norm_url(txt))
        seen, out = set(), []
        for u in urls:
            kind, key = link_kind(u)
            if kind == "web" or u in seen or (key or "").lower() == self.bot.lower():
                continue
            seen.add(u)
            out.append(u)
        return out, verify

    async def _complete(self, url: str, report: dict) -> None:
        kind, key = link_kind(url)
        label = key
        for attempt in range(2):
            try:
                if kind == "invite":
                    res = await self.client(functions.messages.CheckChatInviteRequest(key))
                    label = getattr(getattr(res, "chat", None), "title", None) or getattr(res, "title", key)
                    if isinstance(res, types.ChatInviteAlready):
                        report["already"].append(label)
                        return
                    await self.client(functions.messages.ImportChatInviteRequest(key))
                    report["joined"].append(label)
                elif kind == "public":
                    ent = await self.client.get_entity(key)
                    if isinstance(ent, types.User) and ent.bot:
                        m = re.search(r"[?&]start=([\w-]+)", url)
                        await self.client(functions.messages.StartBotRequest(
                            bot=ent, peer=ent, start_param=m.group(1) if m else "start"))
                        report["bots"].append("@" + (ent.username or key))
                    elif isinstance(ent, types.Channel):
                        label = ent.title
                        if not ent.left:
                            report["already"].append(label)
                            return
                        await self.client(functions.channels.JoinChannelRequest(ent))
                        report["joined"].append(label)
                    else:
                        report["skipped"].append(label)
                else:
                    report["failed"].append(f"{label} (link privato senza invito)")
                return
            except errors.UserAlreadyParticipantError:
                report["already"].append(label)
                return
            except errors.InviteRequestSentError:
                report["requested"].append(label)
                return
            except errors.FloodWaitError as e:
                if attempt == 0 and e.seconds <= 30:
                    await asyncio.sleep(e.seconds + 1)
                    continue
                report["failed"].append(f"{label} (Telegram chiede di aspettare {e.seconds}s)")
                report["stop"] = True
                return
            except errors.ChannelsTooMuchError:
                report["failed"].append(f"{label} (sei già in troppi canali)")
                report["stop"] = True
                return
            except Exception as e:  # noqa: BLE001
                report["failed"].append(f"{label} ({e})")
                return

    async def unlock(self) -> dict:
        """Completa i requisiti del bot: entra nei canali richiesti, avvia i bot indicati, preme Sblocca."""
        report = {"joined": [], "already": [], "requested": [], "bots": [], "skipped": [], "failed": [],
                  "clicked": [], "ok": False, "message": ""}
        async with self._lock:
            bot = await self.client.get_input_entity(self.bot)
            await self.client.send_message(bot, "/start")
            await asyncio.sleep(3)
            done_urls: set[str] = set()
            for _round in range(3):
                msg, gate = await self._state(bot)
                if gate is None:
                    report["ok"] = msg is not None
                    break
                urls, verify = self._gate_links(gate)
                todo = [u for u in urls if u not in done_urls][:MAX_JOINS - len(done_urls)]
                log.info("Sblocco: %d link da completare, %d bottoni di verifica", len(todo), len(verify))
                for u in todo:
                    done_urls.add(u)
                    await self._complete(u, report)
                    if report.get("stop"):
                        break
                    await asyncio.sleep(2)
                if report.get("stop"):
                    break
                await asyncio.sleep(2)
                if verify:
                    for b in verify[:1]:
                        try:
                            ans = await b.click()
                            report["clicked"].append(b.text)
                            if ans is not None and getattr(ans, "message", None):
                                report["message"] = ans.message
                        except Exception as e:  # noqa: BLE001
                            report["failed"].append(f"bottone «{b.text}» ({e})")
                else:
                    await self.client.send_message(bot, "/start")
                await asyncio.sleep(3)
            msg, gate = await self._state(bot)
            self._set_gate(gate)
            report["ok"] = gate is None and msg is not None
        report.pop("stop", None)
        self._resolved.clear()
        log.info("Sblocco: %s", {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()})
        return report

    # ---------------------------------------------------------------- link
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
