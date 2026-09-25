"""``mcsm.lock.json``: exactly what is installed right now.

Only files recorded here are ever deleted or replaced by an upgrade, so jars and
files you add by hand are left alone.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .mods.base import ModFile

LOCK_NAME = "mcsm.lock.json"


@dataclass
class Lock:
    minecraft: str | None = None
    loader: str | None = None
    loader_version: str | None = None
    java_major: int | None = None
    launch: list[str] = field(default_factory=list)
    runtime_files: list[str] = field(default_factory=list)
    mods: list[ModFile] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # mod key -> reason it isn't installed
    failed_plans: dict[str, str] = field(default_factory=dict)  # plan fingerprint -> error
    updated_at: str | None = None

    @property
    def installed(self) -> bool:
        return bool(self.minecraft and self.launch)

    def to_dict(self) -> dict:
        return {
            "minecraft": self.minecraft,
            "loader": self.loader,
            "loader_version": self.loader_version,
            "java_major": self.java_major,
            "launch": self.launch,
            "runtime_files": self.runtime_files,
            "mods": [m.to_dict() for m in self.mods],
            "skipped": self.skipped,
            "failed_plans": self.failed_plans,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Lock:
        return cls(
            minecraft=d.get("minecraft"),
            loader=d.get("loader"),
            loader_version=d.get("loader_version"),
            java_major=d.get("java_major"),
            launch=d.get("launch", []),
            runtime_files=d.get("runtime_files", []),
            mods=[ModFile.from_dict(m) for m in d.get("mods", [])],
            skipped=d.get("skipped", {}),
            failed_plans=d.get("failed_plans", {}),
            updated_at=d.get("updated_at"),
        )


def load(root: Path) -> Lock:
    path = root / LOCK_NAME
    if not path.exists():
        return Lock()
    return Lock.from_dict(json.loads(path.read_text()))


def save(root: Path, lock: Lock, touch: bool = True) -> None:
    if touch:
        lock.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".lock-")
    with os.fdopen(fd, "w") as f:
        json.dump(lock.to_dict(), f, indent=2)
        f.write("\n")
    os.replace(tmp, root / LOCK_NAME)
