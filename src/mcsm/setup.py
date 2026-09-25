"""First-time server setup, shared by the web UI's setup page and the terminal wizard.

`mcsm start` with no server yet writes a placeholder config and marks setup as
*pending*; the web UI then shows the setup page instead of the dashboard, and
nothing is downloaded or started until the person submits it.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config as configmod
from .config import ConfigError, ModSpec
from .properties import write_properties

PENDING = "setup-pending"
NAME_LIMIT = 59  # server.properties motd is shown in the server list; keep it short

LOADER_INFO = [
    ("fabric", "Fabric", "Lightweight and quick to update. Most performance and quality-of-life mods.", True),
    ("neoforge", "NeoForge", "The modern Forge. Big content mods and modpacks for recent versions.", True),
    ("forge", "Forge", "The classic loader. Best for older modpacks (Minecraft 1.20.1 and earlier).", True),
    ("quilt", "Quilt", "A Fabric fork that also runs most Fabric mods.", True),
    ("vanilla", "Vanilla", "Plain Minecraft with no mods.", False),
]
DIFFICULTIES = ("peaceful", "easy", "normal", "hard")
GAMEMODES = ("survival", "creative", "adventure", "spectator")


def pending_path(root: Path) -> Path:
    return root / ".mcsm" / PENDING


def is_pending(root: Path) -> bool:
    return pending_path(root).exists()


def mark_pending(root: Path) -> None:
    path = pending_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("waiting for setup in the web UI\n")


def clear_pending(root: Path) -> None:
    pending_path(root).unlink(missing_ok=True)


def total_ram_gb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.ullTotalPhys / 1024 ** 3
            return None
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / 1024 ** 3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (OSError, ValueError, AttributeError):
        return None


def suggested_memory_gb(total: float | None = None) -> int:
    """Half the computer's memory, between 2 and 8 GB (4 GB if unknown)."""
    total = total_ram_gb() if total is None else total
    if not total:
        return 4
    return int(max(2, min(8, total // 2)))


@dataclass
class SetupSpec:
    loader: str = "fabric"
    minecraft: str = "latest"
    mods: list[str] = field(default_factory=list)            # Modrinth slugs, required
    optional_mods: list[str] = field(default_factory=list)   # Modrinth slugs, optional
    memory_gb: int = 4
    motd: str = "A Minecraft server"
    max_players: int = 20
    difficulty: str = "normal"
    gamemode: str = "survival"
    port: int = 25565
    network_access: bool = False   # let other devices on the network open the control panel
    accept_eula: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> SetupSpec:
        """Build and validate a spec from untrusted input (the web form)."""
        def slugs(key: str) -> list[str]:
            items = d.get(key) or []
            if not isinstance(items, list):
                raise ConfigError(f"{key} must be a list")
            out = []
            for item in items:
                item = str(item).strip()
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", item):
                    raise ConfigError(f"{item!r} is not a valid mod id")
                if item not in out:
                    out.append(item)
            return out

        def number(key: str, lo: int, hi: int, default: int) -> int:
            try:
                value = int(d.get(key, default))
            except (TypeError, ValueError):
                raise ConfigError(f"{key} must be a number") from None
            if not lo <= value <= hi:
                raise ConfigError(f"{key} must be between {lo} and {hi}")
            return value

        spec = cls(
            loader=str(d.get("loader", "fabric")),
            minecraft=str(d.get("minecraft", "latest")).strip() or "latest",
            mods=slugs("mods"),
            optional_mods=slugs("optional_mods"),
            memory_gb=number("memory_gb", 1, 64, 4),
            motd=" ".join(str(d.get("motd", "A Minecraft server")).split())[:NAME_LIMIT] or "A Minecraft server",
            max_players=number("max_players", 1, 1000, 20),
            difficulty=str(d.get("difficulty", "normal")),
            gamemode=str(d.get("gamemode", "survival")),
            port=number("port", 1024, 65535, 25565),
            network_access=bool(d.get("network_access", False)),
            accept_eula=d.get("accept_eula") is True,
        )
        if spec.loader not in configmod.LOADERS:
            raise ConfigError(f"unknown server type {spec.loader!r}")
        if not re.fullmatch(r"latest|\d+(\.\d+){1,3}(-[A-Za-z0-9.]+)?", spec.minecraft):
            raise ConfigError(f"{spec.minecraft!r} is not a Minecraft version")
        if spec.difficulty not in DIFFICULTIES or spec.gamemode not in GAMEMODES:
            raise ConfigError("invalid difficulty or game mode")
        if spec.loader == "vanilla" and (spec.mods or spec.optional_mods):
            raise ConfigError("vanilla servers can't run mods; pick a mod loader or remove the mods")
        if not spec.accept_eula:
            raise ConfigError("you need to accept the Minecraft EULA to run a server")
        # Fabric and Quilt mods almost all need Fabric API; add it unless it's there.
        if spec.loader in ("fabric", "quilt") and (spec.mods or spec.optional_mods) \
                and "fabric-api" not in spec.mods + spec.optional_mods:
            spec.mods.insert(0, "fabric-api")
        return spec


def configure(root: Path, spec: SetupSpec) -> configmod.Config:
    """Write mcsm.toml, server.properties and eula.txt for a new server (downloads nothing)."""
    path = root / configmod.CONFIG_NAME
    web_password = configmod.load(root).web.password if path.exists() else ""
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(configmod.render_template(spec.loader, spec.minecraft))
    configmod.set_value(path, "server", "memory", json.dumps(f"{spec.memory_gb}G"))
    if spec.minecraft != "latest":
        # Choosing a specific version (e.g. for a modpack) means staying on it; mods still update.
        configmod.set_value(path, "updates", "strategy", '"mods-only"')
    if spec.network_access:
        configmod.set_value(path, "web", "host", '"0.0.0.0"')
    if web_password:
        configmod.set_value(path, "web", "password", json.dumps(web_password))
    for slug in spec.mods:
        configmod.append_mod(path, ModSpec("modrinth", slug, required=True))
    for slug in spec.optional_mods:
        configmod.append_mod(path, ModSpec("modrinth", slug, required=False))
    cfg = configmod.load(root)
    cfg.server.dir.mkdir(parents=True, exist_ok=True)
    write_properties(cfg.server.dir / "server.properties", {
        "motd": spec.motd, "max-players": str(spec.max_players), "difficulty": spec.difficulty,
        "gamemode": spec.gamemode, "server-port": str(spec.port),
    })
    if spec.accept_eula:
        (cfg.server.dir / "eula.txt").write_text(
            "# Accepted in the mcsm setup (https://aka.ms/MinecraftEULA)\neula=true\n")
    return cfg
