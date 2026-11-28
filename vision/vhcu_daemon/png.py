"""Pure-Python PNG writer (RGB, no alpha).

Why hand-rolled: P0 must run on a bare Python install with zero third-party
packages. PNG + zlib is the smallest honest image format we can emit for the
preview plane; JPEG would need a codec dependency.

Size matters because every attachment-mode screenshot is stored verbatim in the
host attachment store (which never auto-deletes). Screen content compresses far
better with per-row PNG filters (Sub/Up/Paeth, chosen per row by the standard
sum-of-absolute-values heuristic) than with filter 0.

Measured on a real 1280x720 desktop frame:
  level 3 + filter 0       560,798 B   (old)
  level 6 + per-row filters 455,228 B  ~19% smaller, ~430 ms
  level 9 + per-row filters 443,862 B  only ~2.5% better for +0.8 s of zlib
so level 6 is the sweet spot; level 9 would stall every screenshot for a
negligible gain. numpy (already a daemon dependency) makes the filter choice
vectorised; without numpy we fall back to filter 0 at level 6.
"""

from __future__ import annotations

import struct
import zlib
from typing import Iterable, List

try:  # numpy is a normal daemon dependency, but the encoder must never hard-fail
    import numpy as _np
except Exception:  # pragma: no cover - dependency missing on a bare install
    _np = None

# PNG filter types we consider per row (filter 0 never wins on screen content,
# but it is kept as the first candidate so ties resolve predictably).
_FILTERS = (0, 1, 2, 4)  # none, sub, up, paeth
_ZLIB_LEVEL = 6

_ABS_LUT = None
if _np is not None:  # signed byte magnitude: |v| for two's-complement uint8
    _ABS_LUT = _np.array([v if v < 128 else 256 - v for v in range(256)], dtype=_np.uint16)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def _filter_rows_numpy(rows: List[bytes], width: int, height: int) -> bytes:
    bpp = 3  # RGB
    row_bytes = width * bpp
    data = _np.frombuffer(b"".join(rows), dtype=_np.uint8)
    if data.size != row_bytes * height:
        raise ValueError("row payload does not match width/height")
    pixels = data.reshape(height, row_bytes)

    zeros_col = _np.zeros((height, bpp), dtype=_np.uint8)
    left = _np.concatenate([zeros_col, pixels[:, :-bpp]], axis=1)
    up = _np.vstack([_np.zeros((1, row_bytes), dtype=_np.uint8), pixels[:-1]])
    upleft = _np.concatenate([zeros_col, up[:, :-bpp]], axis=1)

    # uint8 subtraction wraps modulo 256, which is exactly the PNG filter maths.
    f_sub = pixels - left
    f_up = pixels - up

    p = up.astype(_np.int16) + left.astype(_np.int16) - upleft.astype(_np.int16)
    pa = _np.abs(p - up.astype(_np.int16))
    pb = _np.abs(p - left.astype(_np.int16))
    pc = _np.abs(p - upleft.astype(_np.int16))
    predictor = _np.where((pa <= pb) & (pa <= pc), up, _np.where(pb <= pc, left, upleft))
    f_paeth = pixels - predictor

    candidates = (pixels, f_sub, f_up, f_paeth)
    scores = _np.stack([_ABS_LUT[c].sum(axis=1, dtype=_np.uint32) for c in candidates], axis=0)
    choice = scores.argmin(axis=0)

    raw = _np.empty((height, row_bytes + 1), dtype=_np.uint8)
    for index, filter_type in enumerate(_FILTERS):
        mask = choice == index
        if not mask.any():
            continue
        raw[mask, 0] = filter_type
        raw[mask, 1:] = candidates[index][mask]
    return raw.tobytes()


def encode_png(width: int, height: int, rgb_rows: Iterable[bytes]) -> bytes:
    """Encode RGB rows (3 bytes per pixel, no padding) into a PNG byte string."""
    rows = [bytes(row) for row in rgb_rows]
    raw = None
    if _np is not None:
        try:
            raw = _filter_rows_numpy(rows, width, height)
        except Exception:  # pragma: no cover - defensive: never break capture
            raw = None
    if raw is None:
        raw = b"".join(b"\x00" + row for row in rows)

    idat = zlib.compress(raw, level=_ZLIB_LEVEL)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
