"""Trasforma i chunk di una diretta in uno stream HLS (index.m3u8 + segmenti .ts).

Ogni chunk Telegram dura 1 secondo ed è un piccolo file mp4 autonomo: lo si rimuxa
(senza ricodificare, se è già H.264/AAC) in un segmento MPEG-TS con l'orario giusto.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path

from chunks import (CHUNK_MS, ChunkNotReady, NotLive, Resync, Unsupported,
                           build_chunk, parse_chunk)

log = logging.getLogger("streamer")

PLAYLIST_SIZE = 12       # segmenti nella playlist live
IDLE_STOP_S = 45         # si ferma se nessuno guarda da 45 s
RESYNC_EVERY = 30        # ogni 30 chunk controlla di non essere rimasto indietro
PREFETCH = 4             # pezzi scaricati in anticipo, in parallelo
FIRST_CHUNK_WAIT_S = 12  # se il primo pezzo non arriva entro questo tempo, rientro nella diretta
MAX_REJOINS = 3
MAX_LAG_MS = 8000


async def run(*cmd: str, input_bytes: bytes | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(input_bytes)
    return proc.returncode, out, err


async def probe_codecs(path: Path) -> tuple[str | None, str | None]:
    code, out, _ = await run("ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                             "-of", "json", str(path))
    if code != 0:
        return None, None
    v = a = None
    for s in json.loads(out or b"{}").get("streams", []):
        if s.get("codec_type") == "video" and not v:
            v = s.get("codec_name")
        if s.get("codec_type") == "audio" and not a:
            a = s.get("codec_name")
    return v, a


class HlsWriter:
    """Un unico ffmpeg sempre acceso produce l'HLS: ogni pezzo da 1 s viene rimuxato in MPEG-TS con il suo
    orario e passato in coda al convertitore. Così i confini tra i pezzi combaciano (l'audio viene
    raddrizzato con aresample) e i segmenti sono tagliati sui fotogrammi chiave: niente scatti ogni secondo."""

    def __init__(self, out_dir: Path):
        self.dir = out_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.offset_s = 0.0
        self.codec_args: list[str] | None = None
        self.has_audio = True
        self.proc: asyncio.subprocess.Process | None = None
        self._err_task: asyncio.Task | None = None
        self.failures = 0

    async def _decide_codecs(self, src: Path) -> None:
        v, a = await probe_codecs(src)
        args: list[str] = []
        args += ["-c:v", "copy"] if v == "h264" else [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-g", "25"]
        self.has_audio = a is not None
        if a is None:
            args += ["-an"]
        elif a in ("aac", "mp3"):
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", "128k"]
        self.codec_args = args
        log.info("Codec sorgente video=%s audio=%s → %s", v, a, " ".join(args))

    async def _start(self) -> None:
        audio = (["-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k", "-af", "aresample=async=1000:first_pts=0"]
                 if self.has_audio else ["-an"])
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt",
               "-f", "mpegts", "-i", "pipe:0", "-map", "0:v:0?", "-c:v", "copy", *audio,
               "-f", "hls", "-hls_time", "2", "-hls_list_size", str(PLAYLIST_SIZE),
               "-hls_flags", "delete_segments+independent_segments+omit_endlist+temp_file",
               "-hls_segment_filename", str(self.dir / "seg%d.ts"), str(self.dir / "index.m3u8")]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)

        async def errors(proc):
            async for line in proc.stderr:
                log.info("ffmpeg HLS: %s", line.decode(errors="replace").strip()[:200])
        self._err_task = asyncio.create_task(errors(self.proc))

    async def add(self, media: bytes, container: str) -> bool:
        ext = {"mp4": "mp4", "mov": "mp4", "ogg": "ogg", "matroska": "mkv", "webm": "webm"}.get(container, "bin")
        src = self.dir / f"in.{ext}"
        src.write_bytes(media)
        if self.codec_args is None:
            await self._decide_codecs(src)
        code, ts, err = await run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-t", f"{CHUNK_MS / 1000:.3f}",   # come tgcalls: quel che sfora il secondo si scarta
            *self.codec_args, "-muxdelay", "0", "-muxpreload", "0",
            "-output_ts_offset", f"{self.offset_s:.3f}", "-f", "mpegts", "pipe:1")
        if code != 0 or not ts:
            log.warning("ffmpeg non ha letto il pezzo: %s", err.decode(errors="replace")[-300:])
            return False
        if self.proc is None or self.proc.returncode is not None:
            if self.proc is not None:
                log.warning("Il convertitore HLS si è fermato: lo riavvio")
            await self._start()
        try:
            self.proc.stdin.write(ts)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.proc = None
            return False
        self.offset_s += CHUNK_MS / 1000      # orologio nostro: anche dopo un salto il flusso resta continuo
        return True

    def mark_discontinuity(self) -> None:
        pass                                   # l'orologio è continuo: non servono discontinuità

    @property
    def ready(self) -> bool:
        try:
            return (self.dir / "index.m3u8").read_text().count("#EXTINF") >= 3
        except OSError:
            return False

    async def close(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                self.proc.kill()
        if self._err_task:
            self._err_task.cancel()


# --------------------------------------------------------------------------- sources

class TelegramSource:
    """Produce chunk in tempo reale da una diretta Telegram."""

    def __init__(self, reader):
        self.reader = reader
        self.rejoins = 0
        self.time_ms = 0
        self.count = 0
        self.jumped = False
        self.on_status = None
        self._tasks: dict[int, asyncio.Task] = {}

    def _status(self, text: str) -> None:
        if self.on_status:
            self.on_status(text)

    async def open(self) -> None:
        self._status("Entro nella diretta…")
        await self.reader.join()
        self.reader.status_cb = self._status
        self.rejoins = 0
        while True:
            self._status("Cerco il punto di partenza…")
            try:
                self.time_ms = await self.reader.start_ms()
                break
            except (NotLive, Resync) as e:
                if self.rejoins >= MAX_REJOINS:
                    raise
                await self._rejoin(f"punto di partenza non trovato ({e or 'nessun pezzo'})")
        self._status("Scarico il primo pezzo di video…")

    def _clear_prefetch(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        self._tasks.clear()

    async def _fetch_patient(self, t: int) -> bytes:
        """Pezzo futuro: «non ancora pronto» è normale, riprovo finché esce (al massimo 15 s)."""
        waited = 0.0
        while True:
            try:
                return await self.reader.fetch(t)
            except ChunkNotReady:
                if waited > 15:
                    raise
                await asyncio.sleep(0.3)
                waited += 0.3

    async def _take(self, t: int) -> bytes:
        """Restituisce il pezzo t, tenendo già in download i successivi (niente scatti per attese di rete)."""
        if not self.count:
            return await self.reader.fetch(t)
        for old in [x for x in self._tasks if x < t]:
            self._tasks.pop(old).cancel()
        for k in range(PREFETCH):
            tt = t + k * CHUNK_MS
            if tt not in self._tasks:
                self._tasks[tt] = asyncio.create_task(self._fetch_patient(tt))
        return await self._tasks.pop(t)

    async def _rejoin(self, why: str) -> None:
        self.rejoins += 1
        log.info("Rientro nella diretta (%d/%d): %s", self.rejoins, MAX_REJOINS, why)
        self._status(f"Il video non arriva: rientro nella diretta ({self.rejoins}/{MAX_REJOINS})…")
        await self.reader.rejoin()

    async def next(self):
        self.jumped = False
        if self.count and self.count % RESYNC_EVERY == 0:
            try:
                end = await self.reader.stream_end_ms(max_wait=0)
            except Exception:  # noqa: BLE001 - controllo facoltativo
                end = None
            if end and self.time_ms < end - MAX_LAG_MS:
                log.info("Troppo indietro di %d ms: salto avanti", end - self.time_ms)
                self.time_ms = (end // CHUNK_MS) * CHUNK_MS - 2000
                self.jumped = True
                self._clear_prefetch()
        waited = 0.0
        while True:
            try:
                data = await self._take(self.time_ms)
                break
            except ChunkNotReady as e:
                if not self.count:
                    self._status(f"Aspetto il primo pezzo di video… (Telegram: {e or 'non pronto'}, {int(waited)} s)")
                    # Primo pezzo che non arriva: come quando apri la diretta su Telegram, rientro.
                    if waited > FIRST_CHUNK_WAIT_S and self.rejoins < MAX_REJOINS:
                        await self._rejoin(f"nessun pezzo dopo {FIRST_CHUNK_WAIT_S} s ({e})")
                        self.time_ms = await self.reader.start_ms()
                        waited = 0.0
                        continue
                await asyncio.sleep(0.1)
                waited += 0.1
                if waited > 20:
                    try:
                        end = await self.reader.stream_end_ms(max_wait=10)
                    except Exception:  # noqa: BLE001
                        end = -1                     # non sappiamo: riprovo ancora un po'
                    if end is None or waited > 60:
                        raise NotLive()
                    if end != -1:
                        waited = 0.0
            except Resync as e:
                self.resyncs = getattr(self, "resyncs", 0) + 1
                log.info("Risincronizzo (%d): %s", self.resyncs, e)
                self._status(f"Riprovo a sincronizzarmi… (Telegram: {e})")
                self._clear_prefetch()
                if self.resyncs > 8:
                    raise RuntimeError(f"Telegram non dà i pezzi della diretta ({e})")
                await asyncio.sleep(1)
                self.time_ms = await self.reader.start_ms()
                self.jumped = True
        self.time_ms += CHUNK_MS
        self.count += 1
        self.resyncs = 0
        if self.count == 1:
            log.info("Primo pezzo della diretta ricevuto (%d byte)", len(data))
        return parse_chunk(data) if data else None

    async def close(self) -> None:
        self._clear_prefetch()
        await self.reader.leave()


class DemoSource:
    """Genera un finto canale (barre colorate + tono) per provare l'app senza Telegram."""

    def __init__(self, slot: int, tmp: Path, name: str = ""):
        self.slot = slot
        self.tmp = tmp
        safe = "".join(ch for ch in name.upper() if ch.isalnum() or ch == " ")[:20].strip()
        self.label = safe or f"CANALE {slot}"
        self.next_at = 0.0
        self.jumped = False
        self.drawtext = True

    async def open(self) -> None:
        self.next_at = time.monotonic()

    async def _render(self) -> bytes:
        out = self.tmp / "demo.mp4"
        hue = (self.slot * 67) % 360
        vf = f"hue=h={hue}"
        if self.drawtext:
            vf += (f",drawtext=text='DEMO {self.label}   %{{localtime\\:%X}}':"
                   "fontcolor=white:fontsize=56:box=1:boxcolor=black@0.55:boxborderw=18:x=40:y=40")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", "testsrc2=size=960x540:rate=25",
               "-f", "lavfi", "-i", f"sine=frequency={330 + self.slot * 110}:sample_rate=48000",
               "-t", "1", "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-f", "mp4", str(out)]
        code, _, err = await run(*cmd)
        if code != 0 and self.drawtext:
            log.info("drawtext non disponibile, demo senza scritta")
            self.drawtext = False
            return await self._render()
        if code != 0:
            raise RuntimeError(err.decode(errors="replace"))
        return out.read_bytes()

    async def next(self):
        delay = self.next_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.next_at = max(self.next_at, time.monotonic() - 1) + CHUNK_MS / 1000
        # Passa dallo stesso parser dei chunk veri.
        return parse_chunk(build_chunk(await self._render()))

    async def close(self) -> None:
        pass


# --------------------------------------------------------------------------- per-slot streamer

class SlotStreamer:
    def __init__(self, slot: int, root: Path, make_source):
        self.slot = slot
        self.root = root
        self.make_source = make_source
        self.dir = root / str(slot)
        self.state = "idle"        # idle | starting | live | offline | unsupported | error
        self.message = ""
        self.last_access = time.monotonic()
        self.task: asyncio.Task | None = None
        self.writer: HlsWriter | None = None

    def touch(self) -> None:
        self.last_access = time.monotonic()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        self.touch()
        if self.running:
            return
        self.state, self.message = "starting", "Mi collego alla diretta…"
        self.task = asyncio.create_task(self._run(), name=f"slot{self.slot}")

    async def stop(self) -> None:
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.writer = HlsWriter(self.dir)
        tmp = Path(tempfile.mkdtemp(prefix=f"cs{self.slot}-"))
        source = None
        try:
            source = self.make_source(self.slot, tmp)
            if hasattr(source, "on_status"):
                def on_status(text):
                    if self.state == "starting":
                        self.message = text
                source.on_status = on_status
            await source.open()
            failures = 0
            while True:
                if time.monotonic() - self.last_access > IDLE_STOP_S:
                    log.info("Canale %s: nessuno guarda, mi fermo", self.slot)
                    self.state, self.message = "idle", ""
                    return
                chunk = await source.next()
                if source.jumped:
                    self.writer.mark_discontinuity()
                if chunk is None:
                    self.writer.mark_discontinuity()
                    continue
                ok = await self.writer.add(chunk.media, chunk.container)
                failures = 0 if ok else failures + 1
                if failures >= 10:
                    raise RuntimeError("ffmpeg non riesce a leggere i chunk della diretta")
                if self.writer.ready and self.state != "live":
                    self.state, self.message = "live", ""
        except asyncio.CancelledError:
            self.state, self.message = "idle", ""
            raise
        except NotLive:
            self.state, self.message = "offline", "Nessuna diretta in corso su questo canale."
        except Unsupported as e:
            self.state, self.message = "unsupported", str(e)
        except Exception as e:  # noqa: BLE001
            log.exception("Canale %s: errore", self.slot)
            if type(e).__name__ == "FloodWaitError":
                msg = f"Telegram ha chiesto di aspettare {getattr(e, 'seconds', '?')} secondi. Tocca di nuovo il canale tra poco."
            else:
                msg = f"Errore: {e}"
            self.state, self.message = "error", msg
        finally:
            if self.writer is not None:
                await self.writer.close()
            if source is not None:
                try:
                    await source.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
            if self.state != "live":
                shutil.rmtree(self.dir, ignore_errors=True)
            else:
                self.state = "idle"
                shutil.rmtree(self.dir, ignore_errors=True)
