from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..http import HttpClient, HttpError
from ..minecraft import Mojang


class LoaderError(Exception):
    pass


@dataclass
class Runtime:
    """What a loader installed and how to launch it.

    ``files`` are the top-level entries (relative to the server dir) owned by the
    loader. They are replaced wholesale on upgrade; everything else is left alone.
    ``launch`` is the argument list that follows ``java <jvm args>``.
    """
    files: list[str]
    launch: list[str]
    extra: dict = field(default_factory=dict)


class Loader(ABC):
    name: str = ""
    #: loader names that mod platforms use for mods that run on this loader
    mod_loaders: tuple[str, ...] = ()
    #: where the server loads mods (plugins, for Paper) from
    mods_folder: str = "mods"

    def __init__(self, http: HttpClient, mojang: Mojang):
        self.http = http
        self.mojang = mojang

    @abstractmethod
    def latest_version(self, minecraft: str) -> str | None:
        """Newest stable loader version for ``minecraft``, or None if unsupported so far."""

    @abstractmethod
    def install(self, minecraft: str, version: str, dest: Path, java: str) -> Runtime:
        """Install the server runtime into the empty directory ``dest``."""

    def _safe_latest(self, fn) -> str | None:
        try:
            return fn()
        except HttpError as e:
            if e.status is not None and 400 <= e.status < 500:
                return None
            raise


def run_installer(java: str, installer: Path, args: list[str], cwd: Path) -> None:
    from ..desktop import NO_WINDOW
    proc = subprocess.run([java, "-jar", str(installer), *args], cwd=cwd,
                          capture_output=True, text=True, **NO_WINDOW)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-20:])
        raise LoaderError(f"installer {installer.name} failed (exit {proc.returncode}):\n{tail}")
    installer.unlink(missing_ok=True)


def args_file_name() -> str:
    return "win_args.txt" if os.name == "nt" else "unix_args.txt"


def version_key(v: str) -> tuple:
    """Sort key for dotted versions like ``21.1.172`` or ``21.1.0-beta``."""
    base, _, pre = v.partition("-")
    nums = []
    for part in base.split("."):
        nums.append(int(part) if part.isdigit() else -1)
    # A pre-release sorts before the matching final release.
    return (tuple(nums), pre == "", pre)
