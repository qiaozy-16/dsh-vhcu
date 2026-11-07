"""Pure-Python PNG writer (RGB, no alpha).

Why hand-rolled: P0 must run on a bare Python install with zero third-party
packages. PNG + zlib is the smallest honest image format we can emit for the
preview plane; JPEG would need a codec dependency.
"""

from __future__ import annotations

import struct
import zlib
from typing import Iterable


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, rgb_rows: Iterable[bytes]) -> bytes:
    """Encode RGB rows (3 bytes per pixel, no padding) into a PNG byte string."""
    raw = b"".join(b"\x00" + bytes(row) for row in rgb_rows)
    idat = zlib.compress(raw, level=3)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
