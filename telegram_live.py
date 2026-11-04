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
JOIN_SETTLE_S = 10             # secondi dopo l'ingresso in cui Telegram può rispondere «non valido» per pezzi validi
FETCH_TIMEOUT_S = 8             # una richiesta di pezzo che non torna entro 8 s è persa: si riapre la connessione
CHANNELS_MIN_GAP = 4.0
_channels_lock = asyncio.Lock()
_last_channels_call = 0.0

# Ultimo punto noto di ogni diretta: {id della chiamata: (tempo del pezzo in ms, quando l'abbiamo visto)}.
# Serve a ripartire subito (anche dopo un FLOOD_WAIT) senza chiedere a Telegram dove è arrivato lo stream:
# lo stream avanza in tempo reale, quindi basta sommare i secondi passati.
_EDGE: dict[int, tuple[int, float]] = {}


def _remember_edge(call_id: int, t: int) -> None:
    old = _EDGE.get(call_id)
    now = time.monotonic()
    if not old or t >= old[0] + (now - old[1]) * 1000 - 3000:
        _EDGE[call_id] = (t, now)


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

    def drop_sender(self, dc_id: int | None) -> None:
        """Connessione allo stream caduta o bloccata: la chiudo, la prossima richiesta ne apre una nuova."""
        sender = self._media_senders.pop(dc_id, None) if dc_id is not None else None
        if sender is not None:
            asyncio.ensure_future(sender.disconnect())

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
        self.quick = False
        self._join_lock = asyncio.Lock()
        self._joined_at = 0.0

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
        async with self._join_lock:
            await self._join()

    async def rejoin_if_stale(self, since: float) -> None:
        """Rientra solo se nessun altro l'ha già fatto dopo `since`: con 4 pezzi scaricati in parallelo,
        un «non sei nella diretta» arriva a tutti insieme, ma basta rientrare una volta."""
        async with self._join_lock:
            if self._joined_at > since:
                return
            log.info("Rientro nella diretta (Telegram dice che non ci sono)")
            await self._join()

    async def _join(self) -> None:
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
        self._joined_at = time.monotonic()
        if not self.quick:                      # lettura di un solo fotogramma: niente controllo (risparmia 1 s)
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
        async with self._join_lock:
            await self.leave()
            await asyncio.sleep(1.5)
            await self._join()

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
        if not getattr(self, "_logged_channels", False):
            self._logged_channels = True
            log.info("Canali dello stream: %s", [(c.channel, c.scale, c.last_timestamp_ms) for c in res.channels])
        end = max(c.last_timestamp_ms for c in res.channels)
        _remember_edge(self.info.call.id, end)
        return end

    def estimate_edge(self, max_age_s: float = 3 * 3600) -> int | None:
        """Dove dovrebbe essere ora la diretta, dall'ultimo pezzo visto (None se non l'abbiamo mai vista)."""
        e = _EDGE.get(self.info.call.id) if self.info else None
        if not e or time.monotonic() - e[1] > max_age_s:
            return None
        return int(e[0] + (time.monotonic() - e[1]) * 1000)

    def server_now_ms(self) -> int:
        offset = getattr(getattr(getattr(self.client, "_sender", None), "_state", None), "time_offset", 0) or 0
        return int((time.time() + offset) * 1000)

    async def start_ms(self, buffer_ms: int = 3000) -> int:
        """Punto di partenza della diretta, nel modo più veloce possibile:
        1. se l'abbiamo vista da poco (lettura dei nomi, diretta di prima) stimo dove è arrivata e verifico con un pezzo;
        2. altrimenti lo chiedo a Telegram, aspettando la sua pausa (di solito 3 s) invece di tirare a indovinare;
        3. solo se non risponde, cerco il punto giusto pezzo per pezzo."""
        est = self.estimate_edge()
        if est is not None:
            t = (est // CHUNK_MS) * CHUNK_MS - buffer_ms
            for back in (0, 2000, 5000):
                if await self._probe(t - back) == "ok":
                    log.info("Riparto dal punto stimato della diretta (%s)", t - back)
                    return t - back
        end = -1
        for attempt in range(3):
            try:
                end = await self.stream_end_ms(max_wait=8)
                log.info("Fine dello stream: %s", end)
            except errors.FloodWaitError as e:
                log.info("Fine dello stream non disponibile (Telegram dice %ss): la cerco", e.seconds)
                break
            if end:
                break
            if time.monotonic() - self._joined_at > JOIN_SETTLE_S:
                break
            await asyncio.sleep(1.5)                     # appena entrati Telegram può dire «nessuno stream»: riprovo
        if end and end > 0:
            base = (end // CHUNK_MS) * CHUNK_MS
            for back in (buffer_ms, 1000, 0, 5000):      # il primo pezzo valido vicino alla fine
                if await self._probe(base - back) == "ok":
                    return base - back
            return base - buffer_ms
        edge = await self.find_live_edge(self.server_now_ms() - buffer_ms, getattr(self, "status_cb", None))
        return edge - 2 * CHUNK_MS

    async def _probe(self, t: int) -> str:
        """Prova a scaricare il pezzo al tempo t: 'ok', 'big' (troppo avanti), 'small:…' (altro errore)."""
        for _ in range(8):
            try:
                await self.fetch(t, first_only=True)
                return "ok"
            except ChunkNotReady as e:
                reason = str(e)
                if "TIME_TOO_BIG" in reason:
                    return "big"
                m = re.search(r"FLOOD_WAIT_(\d+)", reason)
                if m:                                   # pausa chiesta da Telegram: la rispetto
                    await asyncio.sleep(min(int(m.group(1)) + 0.3, 6))
                elif "APPENA_ENTRATO" in reason or reason in ("RETE", "TIMEOUT"):
                    await asyncio.sleep(1.0)            # appena entrati o rete: riprovo tra un secondo
                else:
                    return "small:" + reason
            except Resync as e:
                return "small:" + str(e)
        return "flood"

    async def _find_relative(self) -> int:
        """Ultimo pezzo di una diretta con l'orologio relativo (ms dall'inizio): raddoppio finché è troppo avanti,
        poi cerco il confine in mezzo. Circa 20 richieste anche per una diretta in corso da ore."""
        lo, hi = 0, CHUNK_MS
        while True:
            r = await self._probe(hi)
            log.info("Ricerca diretta (orologio relativo): %s → %s", hi, r)
            if r == "flood":
                raise Resync("Telegram chiede di rallentare")
            if r == "big" or "TIME_INVALID" in r:
                break
            lo = hi
            hi *= 2
            if hi > 400 * 3600 * 1000:
                raise NotLive()
        while hi - lo > CHUNK_MS:
            mid = ((lo + hi) // 2 // CHUNK_MS) * CHUNK_MS
            r = await self._probe(mid)
            if r == "flood":
                raise Resync("Telegram chiede di rallentare")
            if r == "big" or "TIME_INVALID" in r:
                hi = mid
            else:
                lo = mid
        for back in range(0, 6000, CHUNK_MS):
            if await self._probe(lo - back) == "ok":
                return lo - back
        raise NotLive()

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
        if a > 10**12 and r.startswith("small:"):
            # L'ora di oggi non è valida: questa diretta conta il tempo dall'inizio della trasmissione.
            say("Cerco il punto giusto della diretta…")
            return await self._find_relative()
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
        """Scarica il pezzo time_ms. Problemi passeggeri (rete, server lento, «aspetta») diventano
        ChunkNotReady, così chi chiama riprova invece di fermare la diretta."""
        location = types.InputGroupCallStream(
            call=self.info.call, time_ms=time_ms, scale=0,
            video_channel=1, video_quality=self.quality,
        )
        dc = (self.info.dc_id if self.info else None) or self.client.session.dc_id
        data = b""
        offset = 0
        while True:
            started = time.monotonic()
            try:
                res = await asyncio.wait_for(self._invoke_stream_dc(
                    functions.upload.GetFileRequest(location=location, offset=offset, limit=GETFILE_LIMIT)), FETCH_TIMEOUT_S)
            except asyncio.TimeoutError as e:
                log.info("Chunk %s: Telegram non risponde da %ss, riapro la connessione", time_ms, FETCH_TIMEOUT_S)
                self.live.drop_sender(dc)
                raise ChunkNotReady("TIMEOUT") from e
            except errors.FloodWaitError as e:
                raise ChunkNotReady(f"FLOOD_WAIT_{e.seconds}") from e
            except errors.RPCError as e:
                msg = _rpc_message(e)
                if msg != getattr(self, "_last_err", None):
                    self._last_err = msg
                    log.info("Chunk %s: errore %s %s %s", time_ms, type(e).__name__, getattr(e, "code", ""), msg)
                if "TIME_TOO_BIG" in msg or "FLOOD" in msg:
                    raise ChunkNotReady(msg) from e
                if ("TIME_INVALID" in msg or "VIDEO_CHANNEL_INVALID" in msg) and time.monotonic() - self._joined_at < JOIN_SETTLE_S:
                    # Appena entrati, per qualche secondo Telegram rifiuta anche pezzi che esistono: si riprova lì.
                    raise ChunkNotReady("APPENA_ENTRATO " + msg) from e
                # Telegram a volte manda «non sei nella diretta» con il testo generico BAD_REQUEST:
                # lo riconosco anche dal tipo di errore, e rientro invece di risincronizzarmi all'infinito.
                kind = type(e).__name__.upper()
                if ("GROUPCALL_JOIN_MISSING" in msg or "GROUPCALL_FORBIDDEN" in msg
                        or "JOINMISSING" in kind or "GROUPCALLFORBIDDEN" in kind):
                    await self.rejoin_if_stale(started)
                    raise ChunkNotReady(msg) from e
                if (getattr(e, "code", 0) or 0) >= 500 or "INTERNAL" in msg or "TIMEOUT" in msg:
                    raise ChunkNotReady(msg) from e     # errore del server Telegram: si riprova
                raise Resync(msg) from e
            except (ConnectionError, OSError, IOError) as e:
                log.info("Chunk %s: connessione allo stream caduta (%s), la riapro", time_ms, e)
                self.live.drop_sender(dc)
                raise ChunkNotReady("RETE") from e
            if isinstance(res, types.upload.FileCdnRedirect):
                raise Resync("redirect CDN")
            data += res.bytes
            if first_only or len(res.bytes) < GETFILE_LIMIT:
                _remember_edge(self.info.call.id, time_ms)
                return data
            offset += GETFILE_LIMIT
