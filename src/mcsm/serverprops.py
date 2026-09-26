"""The "advanced" server.properties settings the web UI lets you change, with safe limits.

The basics (name, players, difficulty, game mode, port) have their own fields; RCON
and query settings are left out on purpose (mcsm manages RCON itself).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ConfigError


@dataclass(frozen=True)
class Prop:
    key: str
    label: str
    kind: str                 # bool | int | choice | text
    default: str
    group: str
    help: str = ""
    lo: int = 0
    hi: int = 0
    choices: tuple[str, ...] = ()
    max_len: int = 0
    pattern: str = ""


PROPS: list[Prop] = [
    # World
    Prop("level-seed", "World seed", "text", "", "World", "Leave empty for a random world.", max_len=64),
    Prop("level-type", "World type", "choice", "minecraft:normal", "World", "",
         choices=("minecraft:normal", "minecraft:flat", "minecraft:large_biomes", "minecraft:amplified",
                  "minecraft:single_biome_surface")),
    Prop("level-name", "World folder name", "text", "world", "World",
         "Changing it starts a new world (the old one stays in its folder).", max_len=64,
         pattern=r"[A-Za-z0-9 _.-]+"),
    Prop("generate-structures", "Generate villages, temples and other structures", "bool", "true", "World"),
    Prop("hardcore", "Hardcore (one life; the world is locked to hard)", "bool", "false", "World"),
    Prop("allow-nether", "Allow the Nether", "bool", "true", "World"),
    Prop("spawn-protection", "Spawn protection radius (blocks)", "int", "16", "World",
         "Only operators can build this close to spawn. 0 turns it off.", lo=0, hi=1000),
    Prop("max-world-size", "World border radius (blocks)", "int", "29999984", "World", lo=1, hi=29999984),
    # Gameplay
    Prop("pvp", "Players can hurt each other (PvP)", "bool", "true", "Gameplay"),
    Prop("spawn-monsters", "Spawn monsters", "bool", "true", "Gameplay"),
    Prop("spawn-animals", "Spawn animals", "bool", "true", "Gameplay"),
    Prop("spawn-npcs", "Spawn villagers", "bool", "true", "Gameplay"),
    Prop("allow-flight", "Allow flying (needed by some mods; otherwise flyers get kicked)", "bool", "false", "Gameplay"),
    Prop("force-gamemode", "Put players back in the default game mode when they join", "bool", "false", "Gameplay"),
    Prop("enable-command-block", "Enable command blocks", "bool", "false", "Gameplay"),
    Prop("player-idle-timeout", "Kick idle players after (minutes)", "int", "0", "Gameplay", "0 never kicks.",
         lo=0, hi=1440),
    # Players & access
    Prop("white-list", "Whitelist: only listed players can join", "bool", "false", "Players & access",
         "Manage the list on the Players page."),
    Prop("enforce-whitelist", "Kick players who aren't on the whitelist when it's turned on", "bool", "false",
         "Players & access"),
    Prop("online-mode", "Check accounts with Mojang (online mode)", "bool", "true", "Players & access",
         "Leave on. Off lets anyone join with any name, including yours."),
    Prop("enforce-secure-profile", "Require signed chat (secure profiles)", "bool", "true", "Players & access"),
    Prop("hide-online-players", "Hide the player list from the server browser", "bool", "false", "Players & access"),
    Prop("enable-status", "Show the server as online in the server browser", "bool", "true", "Players & access"),
    Prop("op-permission-level", "What operators can do (1-4)", "int", "4", "Players & access",
         "4 = everything, 3 = most commands, 2 = command blocks and cheats, 1 = bypass spawn protection.",
         lo=1, hi=4),
    # Performance
    Prop("view-distance", "View distance (chunks)", "int", "10", "Performance",
         "Lower it if the server lags.", lo=3, hi=32),
    Prop("simulation-distance", "Simulation distance (chunks)", "int", "10", "Performance",
         "How far away crops grow and mobs move.", lo=3, hi=32),
    Prop("entity-broadcast-range-percentage", "Entity visibility range (%)", "int", "100", "Performance",
         lo=10, hi=1000),
    Prop("network-compression-threshold", "Network compression threshold (bytes)", "int", "256", "Performance",
         "-1 turns compression off.", lo=-1, hi=65535),
    Prop("max-tick-time", "Watchdog: stop a frozen server after (ms)", "int", "60000", "Performance",
         "-1 turns the watchdog off.", lo=-1, hi=600000),
    Prop("sync-chunk-writes", "Save chunks immediately (safer, slower)", "bool", "true", "Performance"),
    # Resource pack
    Prop("resource-pack", "Resource pack download URL", "text", "", "Resource pack", max_len=500,
         pattern=r"(https?://\S+)?"),
    Prop("resource-pack-sha1", "Resource pack SHA-1 (optional)", "text", "", "Resource pack", max_len=40,
         pattern=r"[0-9a-fA-F]{40}|"),
    Prop("require-resource-pack", "Players must accept the resource pack", "bool", "false", "Resource pack"),
]
BY_KEY = {p.key: p for p in PROPS}


def schema() -> list[dict]:
    return [{"key": p.key, "label": p.label, "kind": p.kind, "default": p.default, "group": p.group,
             "help": p.help, "min": p.lo, "max": p.hi, "choices": list(p.choices), "max_len": p.max_len}
            for p in PROPS]


def validate(values: object) -> dict[str, str]:
    """Check settings from the web form; returns them as server.properties strings."""
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ConfigError("advanced settings must be an object")
    out: dict[str, str] = {}
    for key, value in values.items():
        p = BY_KEY.get(key)
        if p is None:
            raise ConfigError(f"{key} can't be changed here")
        if p.kind == "bool":
            if value not in (True, False, "true", "false"):
                raise ConfigError(f"{p.label}: must be on or off")
            out[key] = "true" if value in (True, "true") else "false"
        elif p.kind == "int":
            try:
                n = int(str(value).strip())
            except ValueError:
                raise ConfigError(f"{p.label}: must be a whole number") from None
            if not p.lo <= n <= p.hi:
                raise ConfigError(f"{p.label}: must be between {p.lo} and {p.hi}")
            out[key] = str(n)
        elif p.kind == "choice":
            if value not in p.choices:
                raise ConfigError(f"{p.label}: pick one of the listed options")
            out[key] = str(value)
        else:
            text = str(value).strip()
            if "\n" in text or "\r" in text or len(text) > p.max_len:
                raise ConfigError(f"{p.label}: too long, or has a line break")
            if p.pattern and not re.fullmatch(p.pattern, text):
                raise ConfigError(f"{p.label}: that doesn't look right")
            if key == "level-name" and (not text or text in (".", "..")):
                raise ConfigError(f"{p.label}: pick a name")
            out[key] = text
    return out


def current(props: dict[str, str]) -> dict[str, str]:
    """The value of every advanced setting in a server.properties (defaults where unset)."""
    return {p.key: props.get(p.key, p.default) for p in PROPS}
