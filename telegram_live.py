"""Lettura delle dirette Telegram (modalità RTMP / stream) con un account utente.

Come funziona, in breve (vedi https://core.telegram.org/api/group-calls):
1. Dal canale si ricava la chiamata di gruppo attiva (ChannelFull.call).
2. Ci si unisce come ascoltatore muto (phone.joinGroupCall).
3. Sul DC dello stream (groupCall.stream_dc_id) si chiede la fine attuale del flusso
   (phone.getGroupCallStreamChannels) e si scaricano chunk da 1 secondo con
   upload.getFile + inputGroupCallStream (canale video 1 = "unified", audio+video insieme).
4. Ogni chunk ha un piccolo header Telegram (firma 0xa12e810d) seguito da un file
   contenitore (di solito mp4), che poi viene passato a ffmpeg.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import secrets
import time
from dataclasses import dataclass

from telethon import TelegramClient, errors, utils
from telethon.network import MTProtoSender
from telethon.tl import functions, types
from telethon.tl.alltlobjects import LAYER

from chunks import CHUNK_MS, ChunkNotReady, NotLive, Resync, Unsupported  # noqa: F401

log = logging.getLogger("telegram_live")

GETFILE_LIMIT = 128 * 1024      # come Telegram Desktop per i pezzi delle dirette
CHANNELS_MIN_GAP = 8.0
_channels_lock = asyncio.Lock()
_last_channels_call = 0.0


# --------------------------------------------------------------------------- live lookup

@dataclass
class LiveInfo:
    call: types.InputGroupCall
    rtmp: bool
    dc_id: int | None
    title: str | None
    participants: int


def _rpc_message(e: Exception) -> str:
    return (getattr(e, "message", "") or str(e)).upper()


class TelegramLive:
    def __init__(self, client: TelegramClient):
        self.client = client
        self._media_senders: dict[int, MTProtoSender] = {}
        self._media_lock = asyncio.Lock()
        # Le attese per "flood" le gestiamo noi: per i chunk significano solo "non ancora pronto".
        self.client.flood_sleep_threshold = 0

    async def list_channels(self) -> list[dict]:
        out = []
        async for d in self.client.iter_dialogs():
            ent = d.entity
            if isinstance(ent, types.Channel):
                out.append({
                    "peer_id": utils.get_peer_id(ent),
                    "title": d.name or ent.title,
                    "kind": "canale" if ent.broadcast else "gruppo",
                    "username": ent.username,
                })
        out.sort(key=lambda c: c["title"].lower())
        return out

    async def live_info(self, peer_id: int) -> LiveInfo | None:
        entity = await self.client.get_input_entity(peer_id)
        full = await self._retry_flood(functions.channels.GetFullChannelRequest(entity))
        call = getattr(full.full_chat, "call", None)
        if call is None:
            return None
        res = await self._retry_flood(functions.phone.GetGroupCallRequest(call=call, limit=1))
        gc = res.call
        if isinstance(gc, types.GroupCallDiscarded):
            return None
        return LiveInfo(
            call=types.InputGroupCall(id=gc.id, access_hash=gc.access_hash),
            rtmp=bool(getattr(gc, "rtmp_stream", False)),
            dc_id=getattr(gc, "stream_dc_id", None),
            title=getattr(gc, "title", None),
            participants=getattr(gc, "participants_count", 0) or 0,
        )

    async def media_sender(self, dc_id: int) -> MTProtoSender:
        """Connessione ai server «media» del DC dello stream, come fanno le app ufficiali per le dirette
        (sui server normali Telegram risponde di continuo FLOOD_WAIT)."""
        async with self._media_lock:
            sender = self._media_senders.get(dc_id)
            if sender is not None and sender.is_connected():
                return sender
            client = self.client
            cfg = await client(functions.help.GetConfigRequest())
            opts = [o for o in cfg.dc_options if o.id == dc_id and not o.ipv6 and not o.cdn and not o.tcpo_only]
            media = [o for o in opts if o.media_only]
            dc = (media or opts)[0]
            same_dc = dc_id == client.session.dc_id
            sender = MTProtoSender(client.session.auth_key if same_dc else None, loggers=client._log)
            await sender.connect(client._connection(dc.ip_address, dc.port, dc.id, loggers=client._log,
                                                    proxy=client._proxy, local_addr=client._local_addr))
            if same_dc:
                client._init_request.query = functions.help.GetConfigRequest()
            else:
                auth = await client(functions.auth.ExportAuthorizationRequest(dc_id))
                client._init_request.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
            await sender.send(functions.InvokeWithLayerRequest(LAYER, client._init_request))
            log.info("Connessione stream: DC %s, server %s (%s)", dc_id, dc.ip_address,
                     "media" if dc.media_only else "normale, nessun server media disponibile")
            self._media_senders[dc_id] = sender
            return sender

    async def _retry_flood(self, request, attempts: int = 3):
        for i in range(attempts):
            try:
                return await self.client(request)
            except errors.FloodWaitError as e:
                if i == attempts - 1 or e.seconds > 30:
                    raise
                await asyncio.sleep(max(1, e.seconds))

    def reader(self, peer_id: int, quality: int) -> "CallReader":
        return CallReader(self, peer_id, quality)


class CallReader:
    """Un ascoltatore di una diretta: join, lettura chunk, uscita."""

    def __init__(self, live: TelegramLive, peer_id: int, quality: int):
        self.live = live
        self.client = live.client
        self.peer_id = peer_id
        self.quality = max(0, min(2, quality))
        self.info: LiveInfo | None = None
        self.ssrc = 0

    # -- helpers --------------------------------------------------------------------------
    async def _patient(self, call, max_wait: int = 90):
        """Esegue una richiesta aspettando quando Telegram chiede una pausa (FLOOD_WAIT)."""
        waited = 0
        while True:
            try:
                return await call()
            except errors.FloodWaitError as e:
                pause = max(1, e.seconds) + 0.5
                if waited + pause > max_wait:
                    raise
                log.debug("Telegram chiede una pausa di %ss, aspetto", e.seconds)
                await asyncio.sleep(pause)
                waited += pause

    async def _invoke_stream_dc(self, request):
        dc = (self.info.dc_id if self.info else None) or self.client.session.dc_id
        try:
            sender = await self.live.media_sender(dc)
        except Exception as e:  # noqa: BLE001 - ripiego sulla connessione normale
            log.info("Server media non raggiungibili (%s): uso la connessione normale", e)
            sender = None
        if sender is not None:
            return await self.client._call(sender, request)
        if dc == self.client.session.dc_id:
            return await self.client(request)
        exported = await self.client._borrow_exported_sender(dc)
        try:
            return await self.client._call(exported, request)
        finally:
            await self.client._return_exported_sender(exported)

    # -- lifecycle ------------------------------------------------------------------------
    async def join(self) -> None:
        info = await self.live.live_info(self.peer_id)
        if info is None:
            raise NotLive()
        self.info = info
        self.ssrc = random.randint(1, 2**31 - 1)
        fingerprint = ":".join(f"{b:02X}" for b in secrets.token_bytes(32))
        params = {
            "ufrag": secrets.token_hex(2),
            "pwd": secrets.token_urlsafe(18),
            "fingerprints": [{"hash": "sha-256", "setup": "passive", "fingerprint": fingerprint}],   # come tgcalls
            "ssrc": self.ssrc,
        }
        updates = await self._patient(lambda: self.client(functions.phone.JoinGroupCallRequest(
            call=info.call,
            join_as=types.InputPeerSelf(),
            params=types.DataJSON(data=json.dumps(params)),
            muted=True,
            video_stopped=True,
        )))
        mode = {}
        for u in getattr(updates, "updates", []) or []:
            if isinstance(u, types.UpdateGroupCallConnection):
                try:
                    mode = json.loads(u.params.data)
                except ValueError:
                    mode = {}
        if not (info.rtmp or (mode.get("stream") and mode.get("rtmp"))):
            await self.leave()
            raise Unsupported(
                "Questa diretta è una video chat normale (WebRTC), non uno stream: "
                "può essere letta a chunk solo se trasmessa via RTMP o con molti spettatori."
            )
        log.info("Entrato nella diretta %s: rtmp=%s, dc stream=%s, dc account=%s, modalità=%s",
                 self.peer_id, info.rtmp, info.dc_id, self.client.session.dc_id,
                 {k: v for k, v in mode.items() if k in ("stream", "rtmp")})
        if not getattr(self, "quick", False):   # lettura di un solo fotogramma: niente controllo (risparmia 1 s)
            await self.verify_joined()

    async def verify_joined(self) -> bool:
        """Chiede a Telegram se il nostro ingresso risulta registrato (come phone.checkGroupCall dell'app ufficiale)."""
        try:
            await asyncio.sleep(1)
            res = await self.client(functions.phone.CheckGroupCallRequest(call=self.info.call, sources=[self.ssrc]))
            ok = self.ssrc in list(res or [])
            log.info("Controllo ingresso nella diretta: %s", "registrato" if ok else "NON registrato")
            return ok
        except Exception as e:  # noqa: BLE001
            log.info("Controllo ingresso nella diretta non riuscito: %s", e)
            return False

    async def rejoin(self) -> None:
        """Esce e rientra nella diretta: è quello che sblocca lo stream quando il primo ingresso non è valido."""
        log.info("Rientro nella diretta %s", self.peer_id)
        await self.leave()
        await asyncio.sleep(1.5)
        await self.join()

    async def leave(self) -> None:
        if not self.info:
            return
        try:
            await self.client(functions.phone.LeaveGroupCallRequest(call=self.info.call, source=self.ssrc))
        except Exception as e:  # noqa: BLE001 - l'uscita è "best effort"
            log.debug("leave: %s", e)

    async def stream_end_ms(self, max_wait: int = 12) -> int | None:
        """Timestamp (ms) dell'ultimo chunk disponibile, o None se il trasmettitore è fermo.
        Telegram limita molto questa richiesta: al massimo una ogni CHANNELS_MIN_GAP secondi per tutto il server."""
        global _last_channels_call
        async with _channels_lock:
            gap = CHANNELS_MIN_GAP - (time.monotonic() - _last_channels_call)
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                res = await self._patient(lambda: self._invoke_stream_dc(
                    functions.phone.GetGroupCallStreamChannelsRequest(call=self.info.call)), max_wait=max_wait)
            finally:
                _last_channels_call = time.monotonic()
        if not res.channels:
            return None
        return max(c.last_timestamp_ms for c in res.channels)

    def server_now_ms(self) -> int:
        offset = getattr(getattr(getattr(self.client, "_sender", None), "_state", None), "time_offset", 0) or 0
        return int((time.time() + offset) * 1000)

    async def start_ms(self, buffer_ms: int = 3000) -> int:
        """Punto di partenza della diretta. Come Telegram Desktop: se Telegram non dice dove finisce lo
        stream (FLOOD_WAIT o lista vuota), si parte dall'ora attuale del server in millisecondi."""
        try:
            end = await self.stream_end_ms(max_wait=0)
            log.info("Fine dello stream: %s", end)
        except errors.FloodWaitError as e:
            log.info("Fine dello stream non disponibile (Telegram dice %ss): parto dall'ora del server", e.seconds)
            end = None
        if end:
            return (end // CHUNK_MS) * CHUNK_MS - buffer_ms
        edge = await self.find_live_edge(self.server_now_ms() - buffer_ms, getattr(self, "status_cb", None))
        return edge - 2 * CHUNK_MS

    async def _probe(self, t: int) -> str:
        """Prova a scaricare il pezzo al tempo t: 'ok', 'big' (troppo avanti), 'small' (altro errore)."""
        for _ in range(6):
            try:
                await self.fetch(t, first_only=True)
                return "ok"
            except ChunkNotReady as e:
                reason = str(e)
                if "TIME_TOO_BIG" in reason:
                    return "big"
                m = re.search(r"(\d+)", reason)
                await asyncio.sleep(min(int(m.group(1)) + 0.5 if m else 1.0, 5))
            except Resync as e:
                return "small:" + str(e)
        return "flood"

    async def find_live_edge(self, guess: int, status=None) -> int:
        """Trova l'ultimo pezzo disponibile partendo da una stima, senza sapere quale orologio usa la diretta."""
        def say(t):
            if status:
                status(t)
        a = (guess // CHUNK_MS) * CHUNK_MS
        r = await self._probe(a)
        log.info("Ricerca diretta: %s → %s", a, r)
        if r == "ok":
            return a
        if r == "flood":
            raise Resync("Telegram chiede di rallentare")
        say("Cerco il punto giusto della diretta…")
        direction = -1 if r == "big" else 1          # troppo avanti → indietro; errore → avanti
        far, far_r = a, r
        near = None
        step = CHUNK_MS
        for _ in range(45):
            t = a + direction * step
            rt = await self._probe(t)
            log.info("Ricerca diretta: %s → %s", t, rt)
            if rt == "ok":
                near = t
                break
            if rt == "flood":
                raise Resync("Telegram chiede di rallentare")
            if (rt == "big") != (r == "big"):        # attraversato il confine: cerco in mezzo
                lo, hi = (t, a) if direction < 0 else (a, t)
                lo_r = rt if direction < 0 else r
                for _ in range(45):
                    if hi - lo <= CHUNK_MS:
                        break
                    mid = ((lo + hi) // 2 // CHUNK_MS) * CHUNK_MS
                    rm = await self._probe(mid)
                    log.info("Ricerca diretta: %s → %s", mid, rm)
                    if rm == "ok":
                        near = mid
                        break
                    if rm == "big":
                        hi = mid
                    else:
                        lo = mid
                break
            a, step = t, step * 2
        if near is None:
            raise NotLive()
        # Ho un pezzo valido: avanzo fino all'ultimo disponibile.
        lo, hi = near, near + CHUNK_MS
        step = CHUNK_MS
        while True:
            rt = await self._probe(lo + step)
            if rt != "ok":
                hi = lo + step
                break
            lo, step = lo + step, step * 2
        while hi - lo > CHUNK_MS:
            mid = ((lo + hi) // 2 // CHUNK_MS) * CHUNK_MS
            if mid <= lo:
                break
            if await self._probe(mid) == "ok":
                lo = mid
            else:
                hi = mid
        log.info("Ricerca diretta: ultimo pezzo disponibile %s", lo)
        return lo

    async def fetch(self, time_ms: int, first_only: bool = False) -> bytes:
        location = types.InputGroupCallStream(
            call=self.info.call, time_ms=time_ms, scale=0,
            video_channel=1, video_quality=self.quality,
        )
        data = b""
        offset = 0
        while True:
            try:
                res = await self._invoke_stream_dc(
                    functions.upload.GetFileRequest(location=location, offset=offset, limit=GETFILE_LIMIT))
            except errors.FloodWaitError as e:
                raise ChunkNotReady(f"FLOOD_WAIT_{e.seconds}") from e
            except errors.RPCError as e:
                msg = _rpc_message(e)
                if msg != getattr(self, "_last_err", None):
                    self._last_err = msg
                    log.info("Chunk %s: errore %s %s %s", time_ms, type(e).__name__, getattr(e, "code", ""), msg)
                if "TIME_TOO_BIG" in msg or "FLOOD" in msg:
                    raise ChunkNotReady(msg) from e
                if "GROUPCALL_JOIN_MISSING" in msg or "GROUPCALL_FORBIDDEN" in msg:
                    log.info("Rientro nella diretta (%s)", msg)
                    await self.join()
                    raise ChunkNotReady(msg) from e
                raise Resync(msg) from e
            if isinstance(res, types.upload.FileCdnRedirect):
                raise Resync("redirect CDN")
            data += res.bytes
            if first_only or len(res.bytes) < GETFILE_LIMIT:
                return data
            offset += GETFILE_LIMIT
