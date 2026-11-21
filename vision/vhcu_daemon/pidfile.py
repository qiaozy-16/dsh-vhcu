"""PID records for reclaiming VHCU daemons orphaned by a dead host.

One record per daemon lives in ``%LOCALAPPDATA%/dsh-vhcu/run/vhcu-<pid>.json``
(``VHCU_PID_DIR`` overrides the directory for tests). On startup a new daemon
scans the directory and terminates records whose recorded host parent is gone
and whose process identity (image + creation time) still matches. Live hosts
are never touched, so concurrent DSH profiles can share the machine safely.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Optional

PID_RECORD_VERSION = 1

# Development daemons run as python.exe; packaged daemons use vhcu-daemon.exe.
ALLOWED_IMAGES = frozenset({
    "vhcu-daemon.exe",
    "vhcu_daemon.exe",
    "python.exe",
    "pythonw.exe",
    "python3.exe",
})

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
ERROR_INVALID_PARAMETER = 87
ERROR_ACCESS_DENIED = 5

_K32 = None


def _k32():
    """Lazily bind kernel32 with explicit signatures (Windows only)."""
    global _K32
    if _K32 is not None:
        return _K32
    if os.name != "nt":
        return None
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _K32 = kernel32
    return _K32


def run_dir() -> Path:
    override = os.environ.get("VHCU_PID_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(Path.home())
    return Path(base) / "dsh-vhcu" / "run"


def record_path(pid: int) -> Path:
    return run_dir() / f"vhcu-{int(pid)}.json"


def _process_info(pid: int, access: int) -> Optional[dict]:
    """Return image/creation for one live process, else None."""
    kernel32 = _k32()
    if kernel32 is None or pid <= 0:
        return None
    handle = kernel32.OpenProcess(access, False, int(pid))
    if not handle:
        return None
    try:
        from ctypes import wintypes

        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        image = ""
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            image = os.path.basename(buffer.value).lower()
        created = 0
        created_ft = wintypes.FILETIME()
        exited_ft = wintypes.FILETIME()
        kernel_ft = wintypes.FILETIME()
        user_ft = wintypes.FILETIME()
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created_ft),
            ctypes.byref(exited_ft),
            ctypes.byref(kernel_ft),
            ctypes.byref(user_ft),
        ):
            created = (int(created_ft.dwHighDateTime) << 32) | int(created_ft.dwLowDateTime)
        return {"image": image, "createTime": int(created)}
    finally:
        kernel32.CloseHandle(handle)


def _parent_alive(pid: int) -> bool:
    """Unknown parent (0) is treated as alive: never risk a live daemon."""
    if pid <= 0 or pid == os.getpid():
        return True
    kernel32 = _k32()
    if kernel32 is None:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) != WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def write_record(parent_pid: int = 0) -> Path:
    """Write this daemon's identity record atomically."""
    directory = run_dir()
    directory.mkdir(parents=True, exist_ok=True)
    info = _process_info(os.getpid(), PROCESS_QUERY_LIMITED_INFORMATION) or {}
    payload = {
        "version": PID_RECORD_VERSION,
        "pid": os.getpid(),
        "parentPid": int(parent_pid or 0),
        "createTime": int(info.get("createTime") or 0),
        "image": os.path.basename(sys.executable).lower(),
    }
    path = record_path(os.getpid())
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temp, path)
    return path


def remove_record(path: Path | str) -> bool:
    """Remove the record only when it still belongs to this process."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(data.get("pid") or 0) != os.getpid():
            return False
    except Exception:  # noqa: BLE001
        return False
    try:
        Path(path).unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def _terminate(pid: int, expected_creation: int) -> dict:
    kernel32 = _k32()
    if kernel32 is None:
        return {"killed": False, "reason": "windows-only"}
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE,
        False,
        int(pid),
    )
    if not handle:
        # Dead PID reuses are the common case; access denied keeps the record.
        return {"killed": False, "reason": "open-failed", "lastError": ctypes.get_last_error()}
    try:
        from ctypes import wintypes

        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        image = ""
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            image = os.path.basename(buffer.value).lower()
        if image not in ALLOWED_IMAGES:
            return {"killed": False, "reason": f"unexpected-image:{image or 'unknown'}"}
        created = 0
        created_ft = wintypes.FILETIME()
        exited_ft = wintypes.FILETIME()
        kernel_ft = wintypes.FILETIME()
        user_ft = wintypes.FILETIME()
        if kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created_ft),
            ctypes.byref(exited_ft),
            ctypes.byref(kernel_ft),
            ctypes.byref(user_ft),
        ):
            created = (int(created_ft.dwHighDateTime) << 32) | int(created_ft.dwLowDateTime)
        # Never kill without matching both recorded identity fields.
        if not expected_creation or not created or created != expected_creation:
            return {"killed": False, "reason": "identity-mismatch"}
        if not kernel32.TerminateProcess(handle, 1):
            return {"killed": False, "reason": "terminate-failed"}
        kernel32.WaitForSingleObject(handle, 3000)
        return {"killed": True, "reason": "orphan", "image": image}
    finally:
        kernel32.CloseHandle(handle)


def reclaim_orphans() -> list[dict]:
    """Terminate daemons whose host parent died; return one result per record."""
    if os.name != "nt":
        return []
    results: list[dict] = []
    directory = run_dir()
    try:
        entries = sorted(directory.glob("vhcu-*.json"))
    except Exception:  # noqa: BLE001
        return results
    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - malformed record is safe to delete
            try:
                path.unlink()
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            pid = int(data.get("pid") or 0)
            parent = int(data.get("parentPid") or 0)
            expected_creation = int(data.get("createTime") or 0)
        except (TypeError, ValueError):
            try:
                path.unlink()
            except Exception:  # noqa: BLE001
                pass
            continue
        if pid <= 0 or pid == os.getpid() or path.stem != f"vhcu-{pid}":
            continue
        if _parent_alive(parent):
            # A live host still owns this daemon (another profile/session).
            continue
        outcome = _terminate(pid, expected_creation)
        if outcome.get("killed"):
            results.append({"pid": pid, "action": "killed", "reason": outcome.get("reason")})
            try:
                path.unlink()
            except Exception:  # noqa: BLE001
                pass
            continue
        if outcome.get("reason") == "open-failed":
            # Either the process is gone (87) or access was denied (5).
            if outcome.get("lastError") == ERROR_INVALID_PARAMETER:
                try:
                    path.unlink()
                except Exception:  # noqa: BLE001
                    pass
                results.append({"pid": pid, "action": "stale-record", "reason": "process-gone"})
            else:
                results.append({"pid": pid, "action": "ignored", "reason": "access-denied"})
            continue
        results.append({"pid": pid, "action": "ignored", "reason": outcome.get("reason")})
    return results
