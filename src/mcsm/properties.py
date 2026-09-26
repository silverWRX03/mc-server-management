"""Reading and editing ``server.properties`` without disturbing lines mcsm doesn't touch."""

from __future__ import annotations

import os
from pathlib import Path


def read_properties(path: Path) -> dict[str, str]:
    props = {}
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                props[k.strip()] = v.strip()
    return props


def write_properties(path: Path, updates: dict[str, str]) -> None:
    """Set keys in ``server.properties``. Minecraft fills in every other default on first start."""
    lines = path.read_text(errors="replace").splitlines() if path.exists() else []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        if line and not line.startswith("#") and "=" in line:
            key = line.partition("=")[0].strip()
            if key in remaining:
                lines[i] = f"{key}={remaining.pop(key)}"
    lines += [f"{k}={v}" for k, v in remaining.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replace the file in one step, so nothing ever reads it half-written.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)
