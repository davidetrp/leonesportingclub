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

PLAYLIST_SIZE = 8        # segmenti nella playlist live
KEEP_FILES = 24          # segmenti tenuti su disco
IDLE_STOP_S = 45         # si ferma se nessuno guarda da 45 s
RESYNC_EVERY = 30        # ogni 30 chunk controlla di non essere rimasto indietro
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
    def __init__(self, out_dir: Path):
        self.dir = out_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        self.segments: list[tuple[int, float, bool]] = []  # (numero, durata, discontinuità prima)
        self.offset_s = 0.0
        self.codec_args: list[str] | None = None
        self.pending_discontinuity = False

    async def _decide_codecs(self, src: Path) -> None:
        v, a = await probe_codecs(src)
        args: list[str] = []
        args += ["-c:v", "copy"] if v == "h264" else [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"]
        if a is None:
            args += ["-an"]
        elif a in ("aac", "mp3"):
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", "128k"]
        self.codec_args = args
        log.info("Codec sorgente video=%s audio=%s → %s", v, a, " ".join(args))

    async def add(self, media: bytes, container: str) -> bool:
        ext = {"mp4": "mp4", "mov": "mp4", "ogg": "ogg", "matroska": "mkv", "webm": "webm"}.get(container, "bin")
        src = self.dir / f"in.{ext}"
        src.write_bytes(media)
        if self.codec_args is None:
            await self._decide_codecs(src)
        seg_no = self.seq
        out = self.dir / f"seg{seg_no}.ts"
        code, _, err = await run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
            "-t", f"{CHUNK_MS / 1000:.3f}",   # come tgcalls: quel che sfora il secondo si scarta
            *self.codec_args, "-muxdelay", "0", "-muxpreload", "0",
            "-output_ts_offset", f"{self.offset_s:.3f}", "-f", "mpegts", str(out))
        if code != 0 or not out.exists() or out.stat().st_size == 0:
            log.warning("ffmpeg non ha prodotto il segmento: %s", err.decode(errors="replace")[-300:])
            self.pending_discontinuity = True
            return False
        duration = CHUNK_MS / 1000
        self.segments.append((seg_no, duration, self.pending_discontinuity))
        self.pending_discontinuity = False
        self.seq += 1
        self.offset_s += duration
        self._write_playlist()
        old = seg_no - KEEP_FILES
        if old >= 0:
            (self.dir / f"seg{old}.ts").unlink(missing_ok=True)
        return True

    def mark_discontinuity(self) -> None:
        self.pending_discontinuity = True

    def _write_playlist(self) -> None:
        segs = self.segments[-PLAYLIST_SIZE:]
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2",
                 f"#EXT-X-MEDIA-SEQUENCE:{segs[0][0]}"]
        disc_count = sum(1 for s in self.segments[:-len(segs)] if s[2])
        lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{disc_count}")
        for i, (n, dur, disc) in enumerate(segs):
            if disc and i > 0:
                lines.append("#EXT-X-DISCONTINUITY")
            lines += [f"#EXTINF:{dur:.3f},", f"seg{n}.ts"]
        tmp = self.dir / "index.m3u8.tmp"
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(self.dir / "index.m3u8")

    @property
    def ready(self) -> bool:
        return len(self.segments) >= 3


# --------------------------------------------------------------------------- sources

class TelegramSource:
    """Produce chunk in tempo reale da una diretta Telegram."""

    def __init__(self, reader):
        self.reader = reader
        self.time_ms = 0
        self.count = 0
        self.jumped = False

    async def open(self) -> None:
        await self.reader.join()
        self.time_ms = await self.reader.start_ms()

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
        waited = 0.0
        while True:
            try:
                data = await self.reader.fetch(self.time_ms)
                break
            except ChunkNotReady:
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
