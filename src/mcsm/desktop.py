"""Running without a command window (the Windows executable).

The Windows build has no console of its own, so double-clicking it shows only the
browser. Its output goes, in order of preference:

1. wherever it was redirected (a script or CI capturing it);
2. the command prompt it was started from, if any;
3. a log file, ``mcsm.log`` in mcsm's folder (the web UI shows the activity anyway).

Programs mcsm starts (Java, Minecraft, loader installers) are started without a
window too, via ``NO_WINDOW``.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

#: pass to subprocess calls: on Windows, don't open a console window for the child
NO_WINDOW: dict = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW

_windowless = False
LOG_LIMIT = 5 << 20


def windowless() -> bool:
    """True when nobody can see a console (double-clicked, no command window)."""
    return _windowless


def _std_handle_stream(which: int, mode: str):
    """A Python stream for an inherited standard handle (a pipe or file), if there is one."""
    import ctypes
    import msvcrt
    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    handle = kernel32.GetStdHandle(which)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY if "r" in mode else os.O_WRONLY)
        return open(fd, mode, encoding="utf-8", errors="replace", buffering=1, closefd=False)
    except OSError:
        return None


def log_path() -> Path:
    home = Path(os.environ.get("MCSM_HOME") or Path.home() / "mcsm")
    return home / ".mcsm" / "mcsm.log"


def _log_file():
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_suffix(".log.1"))
        return open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return io.StringIO()


def setup() -> None:
    """Give a console-less process somewhere to write. Call before anything prints."""
    global _windowless
    if os.name != "nt" or sys.stdout is not None:
        return
    out = _std_handle_stream(-11, "w")   # STD_OUTPUT_HANDLE
    err = _std_handle_stream(-12, "w") or out
    if out is None:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):  # started from a command prompt
            try:
                out = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
                err = out
                sys.stdout, sys.stderr = out, err
                print(flush=True)  # start on a line of its own after the prompt
                return
            except OSError:
                out = None
    if out is None:
        _windowless = True
        out = err = _log_file()
    sys.stdout, sys.stderr = out, err
    if sys.stdin is None:
        sys.stdin = _std_handle_stream(-10, "r") or io.StringIO()


def show_error(message: str) -> None:
    """A message box, for errors nobody would otherwise see."""
    if not _windowless:
        print(message, file=sys.stderr)
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "mcsm", 0x10)  # MB_ICONERROR
    except Exception:
        pass
