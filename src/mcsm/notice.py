"""The first-run notice: what mcsm does and doesn't do. It must be accepted before use.

Acceptance is stored per OS user (for the command line) and per server directory
(for the web UI); either one counts. Bump ``NOTICE_VERSION`` whenever the points
change, so everyone sees and accepts the new wording.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

NOTICE_VERSION = 2
TITLE = "Before you start: what mcsm does and doesn't do"
POINTS = [
    "mcsm runs your Minecraft server on this computer and keeps it and its mods up to date.",
    "It connects to the internet to download Minecraft, mod loaders, mods, Java and its own updates "
    "(from Mojang, Modrinth, CurseForge, Fabric, Quilt, NeoForge, Forge, Adoptium and GitHub).",
    "It does not collect usage data. There is no analytics, tracking, advertising or account.",
    "Your worlds, settings and backups stay on this computer. Nothing is uploaded, except messages to "
    "Discord if you set up a webhook. If you switch on a friend download, anyone with its link can see the "
    "server's name, address, Minecraft version and mod list.",
    "It changes files in your server folder: it replaces the mods and loader files it installed, "
    "and makes a backup first.",
    "Moving a world to a newer Minecraft version can't be undone. Restoring a backup is the only way back.",
    "Minecraft belongs to Mojang, and each mod belongs to its author. You accept Minecraft's EULA separately.",
    "This software was created with the help of AI (Anthropic's Claude). It is tested, but it may still have mistakes.",
    "It is free, open-source software (Apache 2.0 license), provided as is, with no warranty.",
    "Keep your own copies of anything you can't afford to lose.",
]
assert len(POINTS) <= 10


def user_file() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "mcsm" / "notice-accepted.json"


def root_file(root: Path) -> Path:
    return root / ".mcsm" / "notice-accepted.json"


def _accepted_in(path: Path) -> bool:
    try:
        return json.loads(path.read_text()).get("version", 0) >= NOTICE_VERSION
    except (FileNotFoundError, ValueError, AttributeError):
        return False


def accepted(root: Path | None = None) -> bool:
    return _accepted_in(user_file()) or (root is not None and _accepted_in(root_file(root)))


def accept(root: Path | None = None, by: str = "cli") -> None:
    record = json.dumps({"version": NOTICE_VERSION, "accepted_at": datetime.now(timezone.utc).isoformat(),
                         "by": by}, indent=2)
    targets = [root_file(root)] if root is not None else []
    if by == "cli":
        targets.append(user_file())
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(record + "\n")


def as_text() -> str:
    lines = [TITLE, ""]
    lines += [f"  • {p}" for p in POINTS]
    return "\n".join(lines)
