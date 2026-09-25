"""CPU and memory use of the Minecraft server process, for the dashboard (stdlib only)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time


def _linux(pid: int) -> tuple[int, float]:
    with open(f"/proc/{pid}/stat") as f:
        # The command name (field 2) may contain spaces; everything after its ")" is fixed.
        fields = f.read().rsplit(")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    cpu = (int(fields[11]) + int(fields[12])) / ticks        # utime + stime
    with open(f"/proc/{pid}/statm") as f:
        rss = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    return rss, cpu


def _windows(pid: int) -> tuple[int, float]:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
    if not handle:
        raise OSError("can't open the server process")
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError("GetProcessTimes failed")
        to_s = lambda ft: ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7  # noqa: E731 - 100 ns units
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        if not kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return counters.WorkingSetSize, to_s(kernel) + to_s(user)
    finally:
        kernel32.CloseHandle(handle)


def _ps(pid: int) -> tuple[int, float]:
    out = subprocess.run(["ps", "-o", "rss=,time=", "-p", str(pid)], capture_output=True, text=True,
                         timeout=5).stdout.split()
    if len(out) < 2:
        raise OSError("process not found")
    days, _, clock = out[1].rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    return int(out[0]) * 1024, seconds + int(days or 0) * 86400


def sample(pid: int) -> tuple[int, float]:
    """(resident memory in bytes, total CPU seconds used) for a process."""
    if sys.platform.startswith("linux"):
        return _linux(pid)
    if os.name == "nt":
        return _windows(pid)
    return _ps(pid)


def heap_bytes(memory: str, auto_gb: int) -> int:
    """The -Xmx a memory setting like "6G", "4096M" or "auto" gives Java."""
    m = re.fullmatch(r"(\d+)([MG])", memory.upper())
    if not m:
        return auto_gb * 1024 ** 3
    return int(m.group(1)) * 1024 ** (3 if m.group(2) == "G" else 2)


class Sampler:
    """Turns successive CPU-time readings into a usage percentage (of the whole machine)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last: tuple[int, float, float] | None = None   # pid, wall time, cpu seconds
        self._percent: float | None = None
        self.cpus = os.cpu_count() or 1

    def read(self, pid: int | None) -> dict | None:
        if pid is None:
            with self._lock:
                self._last = self._percent = None
            return None
        try:
            rss, cpu = sample(pid)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None
        now = time.monotonic()
        with self._lock:
            if self._last and self._last[0] == pid and now - self._last[1] >= 0.5:
                used = (cpu - self._last[2]) / (now - self._last[1]) / self.cpus * 100
                self._percent = round(max(0.0, min(100.0, used)), 1)
            if not self._last or self._last[0] != pid or now - self._last[1] >= 0.5:
                self._last = (pid, now, cpu)
            return {"memory_bytes": rss, "cpu_percent": self._percent, "cpus": self.cpus}
