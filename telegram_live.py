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
import secrets
import time
from dataclasses import dataclass

from telethon import TelegramClient, errors, utils
from telethon.tl import functions, types

from chunks import CHUNK_MS, ChunkNotReady, NotLive, Resync, Unsupported  # noqa: F401

log = logging.getLogger("telegram_live")

GETFILE_LIMIT = 1024 * 1024
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
        dc = self.info.dc_id if self.info else None
        if not dc or dc == self.client.session.dc_id:
            return await self.client(request)
        sender = await self.client._borrow_exported_sender(dc)
        try:
            return await self.client._call(sender, request)
        finally:
            await self.client._return_exported_sender(sender)

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
            "fingerprints": [{"hash": "sha-256", "setup": "active", "fingerprint": fingerprint}],
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
        deadline = asyncio.get_running_loop().time() + 75
        while asyncio.get_running_loop().time() < deadline:
            try:
                end = await self.stream_end_ms(max_wait=20)
            except errors.FloodWaitError as e:
                # Il limite reale sembra più lungo di quello dichiarato: aspetto parecchio di più.
                pause = max(10, e.seconds * 3)
                log.info("Fine dello stream non disponibile (Telegram dice %ss), riprovo tra %ss", e.seconds, pause)
                await asyncio.sleep(pause)
                continue
            log.info("Fine dello stream: %s", end)
            if end:
                start = (end // CHUNK_MS) * CHUNK_MS - buffer_ms
                if start > 0:
                    return start
            await asyncio.sleep(3)
        raise NotLive()

    async def fetch(self, time_ms: int) -> bytes:
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
                raise ChunkNotReady() from e
            except errors.RPCError as e:
                msg = _rpc_message(e)
                if msg != getattr(self, "_last_err", None):
                    self._last_err = msg
                    log.info("Chunk %s: errore %s %s %s", time_ms, type(e).__name__, getattr(e, "code", ""), msg)
                if "TIME_TOO_BIG" in msg or "FLOOD" in msg:
                    raise ChunkNotReady() from e
                if "GROUPCALL_JOIN_MISSING" in msg or "GROUPCALL_FORBIDDEN" in msg:
                    log.info("Rientro nella diretta (%s)", msg)
                    await self.join()
                    raise ChunkNotReady() from e
                raise Resync(msg) from e
            if isinstance(res, types.upload.FileCdnRedirect):
                raise Resync("redirect CDN")
            data += res.bytes
            if len(res.bytes) < GETFILE_LIMIT:
                return data
            offset += GETFILE_LIMIT
