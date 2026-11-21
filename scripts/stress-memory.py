"""1000-iteration memory/GDI stress harness for the VHCU daemon.

Run from the repository root:

    py -3 scripts/stress-memory.py --iterations 1000

It deliberately exercises both hot paths:
  - CursorOverlay move/click/drag updates (GDI/GDI+ path);
  - real mss primary-monitor captures (screenshot buffer path).

No input is injected; the overlay is decoration only. RSS uses psutil when
available, otherwise the Windows process-memory API. GDI handle count uses
GetGuiResources.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vision"))

from vhcu_daemon.cursor_overlay import CursorOverlay  # noqa: E402


def rss_bytes() -> int:
    try:
        import psutil  # type: ignore
        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:  # noqa: BLE001 - psutil is optional
        pass
    try:
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        if not psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters),
            ctypes.sizeof(counters),
        ):
            return 0
        return int(counters.WorkingSetSize)
    except Exception:  # noqa: BLE001
        return 0


def gdi_count() -> int:
    try:
        user32 = ctypes.windll.user32
        user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetGuiResources.restype = ctypes.c_uint
        return int(user32.GetGuiResources(ctypes.windll.kernel32.GetCurrentProcess(), 0))
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--no-overlay", action="store_true")
    parser.add_argument("--no-capture", action="store_true")
    args = parser.parse_args()
    count = max(1, int(args.iterations))

    overlay = None if args.no_overlay else CursorOverlay()
    sct = None
    monitor = None
    if not args.no_capture:
        try:
            import mss  # type: ignore
            sct = mss.MSS()
            monitor = sct.monitors[1]
        except Exception as error:  # noqa: BLE001
            print(f"stress: capture disabled ({error})", file=sys.stderr, flush=True)
            sct = None

    # Warm up one-time GDI+/mss initialization before recording the baseline.
    if overlay:
        overlay.show(100, 100, "move")
        time.sleep(0.2)
    if sct:
        shot = sct.grab(monitor)
        del shot
    gc.collect()
    start_rss = rss_bytes()
    start_gdi = gdi_count()
    max_rss = start_rss
    started = time.perf_counter()

    try:
        for index in range(count):
            if overlay:
                x = 200 + (index % 600)
                y = 200 + (index % 400)
                action = "click" if index % 10 == 0 else ("drag" if index % 3 == 0 else "move")
                overlay.show(x, y, action)
            if sct:
                shot = sct.grab(monitor)
                # Explicitly drop the 8MB-class buffer before the next capture.
                del shot
            if index % 50 == 49:
                gc.collect()
                current = rss_bytes()
                if current > max_rss:
                    max_rss = current
            if index and index % 100 == 0:
                print(f"stress: {index}/{count} rss={rss_bytes()/1048576:.1f}MB gdi={gdi_count()}",
                      file=sys.stderr, flush=True)
    finally:
        end_rss = rss_bytes()
        end_gdi = gdi_count()
        overlay_error = getattr(overlay, "last_error", None) if overlay else None
        capture_enabled = sct is not None
        if overlay:
            overlay.hide()
            overlay.close()
        if sct:
            sct.close()
        gc.collect()

    result = {
        "iterations": count,
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "rss_start_mb": round(start_rss / 1048576, 1),
        "rss_end_mb": round(end_rss / 1048576, 1),
        "rss_max_mb": round(max_rss / 1048576, 1),
        "rss_delta_mb": round((end_rss - start_rss) / 1048576, 1),
        "gdi_start": start_gdi,
        "gdi_end": end_gdi,
        "gdi_delta": end_gdi - start_gdi,
        "overlay_error": overlay_error,
        "capture": capture_enabled,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["gdi_end"] <= start_gdi + 2 else 2


if __name__ == "__main__":
    raise SystemExit(main())
