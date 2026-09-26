"""Opening files and folders with this computer's own apps (Explorer, Finder, the file manager)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _spawn(cmd: list[str]) -> bool:
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def open_path(path: Path) -> bool:
    """Open a folder in the file manager, or a file with the app registered for it."""
    path = Path(path)
    if not path.exists():
        return False
    if os.name == "nt":
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _spawn(["open", str(path)])
    opener = shutil.which("xdg-open") or shutil.which("gio")
    if not opener:
        return False
    return _spawn([opener, "open", str(path)] if opener.endswith("gio") else [opener, str(path)])


def reveal(path: Path) -> bool:
    """Show a file selected in its folder (or just open the folder where that isn't possible)."""
    path = Path(path)
    if path.is_file():
        if os.name == "nt":
            return _spawn(["explorer.exe", "/select,", str(path)])
        if sys.platform == "darwin":
            return _spawn(["open", "-R", str(path)])
        path = path.parent
    return open_path(path)


def downloads_dir() -> Path:
    """Where to put files the person should find (a modpack to import...)."""
    d = Path.home() / "Downloads"
    return d if d.is_dir() else Path.home()
