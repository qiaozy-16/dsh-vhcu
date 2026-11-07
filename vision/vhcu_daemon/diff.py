"""Coarse-grid frame differencing with pure Python.

Pipeline (per frame): sample one pixel per cell on a downsampled grid, compare
against the previous frame, merge changed cells into bounding boxes. Sampling
instead of full-pixel diffing is what keeps this at a few milliseconds without
numpy; ROI boxes are refined later by the action layer, not here.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .sources import Frame, default_step


@dataclass
class Roi:
    x: int
    y: int
    w: int
    h: int
    score: float
    hash: str

    def as_dict(self, roi_id: int) -> dict:
        return {
            "id": roi_id,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "score": round(self.score, 4),
            "hash": self.hash,
        }


@dataclass
class DiffResult:
    rois: List[Roi] = field(default_factory=list)
    changed_cells: int = 0
    cell_total: int = 0
    diff_ms: float = 0.0
    step: int = 0


def compare(
    previous: Optional[Frame],
    current: Frame,
    *,
    step: Optional[int] = None,
    threshold: int = 14,
    min_cells: int = 2,
    max_rois: int = 8,
) -> DiffResult:
    started = time.perf_counter()
    if previous is None or previous.width != current.width or previous.height != current.height:
        return DiffResult(step=step or default_step(current.width, current.height))

    width, height = current.width, current.height
    step = step or default_step(width, height)
    cols = (width + step - 1) // step
    rows = (height + step - 1) // step
    changed = bytearray(cols * rows)
    cur, prev = current.data, previous.data
    half = step // 2
    changed_count = 0

    for cy in range(rows):
        y = min(cy * step + half, height - 1)
        row_base = y * width * 4
        for cx in range(cols):
            x = min(cx * step + half, width - 1)
            offset = row_base + x * 4
            if (
                abs(cur[offset] - prev[offset]) > threshold
                or abs(cur[offset + 1] - prev[offset + 1]) > threshold
                or abs(cur[offset + 2] - prev[offset + 2]) > threshold
            ):
                changed[cy * cols + cx] = 1
                changed_count += 1

    roi_list: List[Roi] = []
    if changed_count:
        visited = bytearray(cols * rows)
        for start in range(len(changed)):
            if not changed[start] or visited[start]:
                continue
            stack = [start]
            visited[start] = 1
            min_cx = max_cx = start % cols
            min_cy = max_cy = start // cols
            count = 0
            while stack:
                index = stack.pop()
                count += 1
                cx, cy = index % cols, index // cols
                if cx < min_cx:
                    min_cx = cx
                if cx > max_cx:
                    max_cx = cx
                if cy < min_cy:
                    min_cy = cy
                if cy > max_cy:
                    max_cy = cy
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if 0 <= nx < cols and 0 <= ny < rows:
                        neighbour = ny * cols + nx
                        if changed[neighbour] and not visited[neighbour]:
                            visited[neighbour] = 1
                            stack.append(neighbour)
            if count < min_cells:
                continue
            cells_in_box = (max_cx - min_cx + 1) * (max_cy - min_cy + 1)
            score = count / cells_in_box if cells_in_box else 0.0
            x0 = min_cx * step
            y0 = min_cy * step
            x1 = min(width, (max_cx + 1) * step)
            y1 = min(height, (max_cy + 1) * step)
            roi_list.append(
                Roi(
                    x=x0,
                    y=y0,
                    w=max(1, x1 - x0),
                    h=max(1, y1 - y0),
                    score=min(1.0, score),
                    hash=_roi_hash(prev, cur, width, x0, y0, x1, y1),
                )
            )

    roi_list.sort(key=lambda roi: roi.score * roi.w * roi.h, reverse=True)
    roi_list = roi_list[:max_rois]
    return DiffResult(
        rois=roi_list,
        changed_cells=changed_count,
        cell_total=cols * rows,
        diff_ms=(time.perf_counter() - started) * 1000.0,
        step=step,
    )


def _roi_hash(prev: bytes, cur: bytes, width: int, x0: int, y0: int, x1: int, y1: int) -> str:
    digest = hashlib.blake2s(digest_size=8)
    digest.update(f"{x0},{y0},{x1},{y1}".encode("ascii"))
    stride_y = max(1, (y1 - y0) // 4)
    stride_x = max(1, (x1 - x0) // 4)
    sampled = 0
    for y in range(y0, y1, stride_y):
        for x in range(x0, x1, stride_x):
            offset = (y * width + x) * 4
            digest.update(cur[offset:offset + 3])
            digest.update(prev[offset:offset + 3])
            sampled += 1
            if sampled >= 64:
                break
        if sampled >= 64:
            break
    return digest.hexdigest()
