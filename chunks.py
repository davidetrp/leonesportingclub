"""Formato dei chunk delle dirette Telegram ed eccezioni comuni (nessuna dipendenza esterna)."""
from __future__ import annotations

import struct
from dataclasses import dataclass

CHUNK_SIGNATURE = 0xA12E810D
CHUNK_MS = 1000          # i client ufficiali usano segmenti fissi da 1000 ms (scale = 0)


class NotLive(Exception):
    """Nel canale non c'è una diretta in corso."""


class Unsupported(Exception):
    """La diretta esiste ma non è leggibile a chunk (es. video chat WebRTC piccola)."""


class Resync(Exception):
    """Lo stream va risincronizzato (redirect CDN, salto di tempo, ecc.)."""


class ChunkNotReady(Exception):
    """Il chunk richiesto non è ancora disponibile sul server."""


# --------------------------------------------------------------------------- chunk format

def _round_up(n: int, multiple: int) -> int:
    r = n % multiple
    return n if r == 0 else n + multiple - r


def _read_tl_string(data: bytes, off: int) -> tuple[bytes, int]:
    if off >= len(data):
        raise ValueError("header troncato")
    first = data[off]
    if first == 254:
        if off + 4 > len(data):
            raise ValueError("header troncato")
        length = int.from_bytes(data[off + 1:off + 4], "little")
        off += 4
        padding = _round_up(length, 4) - length
    else:
        length = first
        off += 1
        padding = _round_up(length + 1, 4) - (length + 1)
    if off + length > len(data):
        raise ValueError("stringa oltre la fine del chunk")
    value = data[off:off + length]
    return value, off + length + padding


@dataclass
class Chunk:
    container: str     # es. "mp4"
    media: bytes       # file contenitore pronto per ffmpeg
    rotation: int = 0


def parse_chunk(data: bytes) -> Chunk:
    """Decodifica un chunk di livestream (stesso formato di tgcalls/VideoStreamingPart.cpp)."""
    if len(data) < 8:
        raise ValueError("chunk vuoto")
    (signature,) = struct.unpack_from("<I", data, 0)
    if signature != CHUNK_SIGNATURE:
        # Nessun header: alcuni server restituiscono direttamente il contenitore.
        guess = "mp4" if data[4:8] == b"ftyp" else "ogg" if data[:4] == b"OggS" else "unknown"
        return Chunk(guess, data)
    off = 4
    container, off = _read_tl_string(data, off)
    _active_mask, event_count = struct.unpack_from("<ii", data, off)
    off += 8
    if event_count <= 0:
        raise ValueError("chunk senza eventi")
    (event_offset,) = struct.unpack_from("<i", data, off)
    off += 4
    _endpoint, off = _read_tl_string(data, off)
    rotation, _extra = struct.unpack_from("<ii", data, off)
    off += 8
    # Gli eventi aggiuntivi (se presenti) vengono ignorati, come fa tgcalls.
    body = data[off:]
    if event_offset < 0 or event_offset >= len(body):
        raise ValueError("offset evento non valido")
    return Chunk(container.decode("ascii", "replace") or "mp4", body[event_offset:], rotation)


def build_chunk(media: bytes, container: str = "mp4") -> bytes:
    """Costruisce un chunk nel formato Telegram (usato dalla modalità demo e dai test)."""
    def tl_string(b: bytes) -> bytes:
        if len(b) < 254:
            raw = bytes([len(b)]) + b
        else:
            raw = bytes([254]) + len(b).to_bytes(3, "little") + b
        return raw + b"\0" * (_round_up(len(raw), 4) - len(raw))

    header = struct.pack("<I", CHUNK_SIGNATURE) + tl_string(container.encode())
    header += struct.pack("<ii", 1, 1)                      # active mask, 1 evento
    header += struct.pack("<i", 0) + tl_string(b"unified")  # offset, endpoint
    header += struct.pack("<ii", 0, 0)                      # rotazione, extra
    return header + media
