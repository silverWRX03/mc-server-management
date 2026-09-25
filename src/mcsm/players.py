"""Player management: kick, ban/pardon, op/deop, whitelist.

While the server runs, actions are sent as console commands (the server then
updates its own JSON files). While it is stopped, mcsm edits ``ops.json``,
``banned-players.json``, ``banned-ips.json`` and ``whitelist.json`` directly,
which needs each player's UUID: from ``usercache.json``, Mojang's API, or, for
``online-mode=false`` servers, the offline UUID Minecraft derives from the name.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .http import HttpClient, HttpError
from .properties import read_properties, write_properties

MOJANG_PROFILE = "https://api.mojang.com/users/profiles/minecraft"
NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
FILES = {"ops": "ops.json", "bans": "banned-players.json", "ip_bans": "banned-ips.json",
         "whitelist": "whitelist.json", "usercache": "usercache.json"}

#: action -> (needs a running server, command template)
ACTIONS = {
    "kick": (True, "kick {name}{reason}"),
    "ban": (False, "ban {name}{reason}"),
    "pardon": (False, "pardon {name}"),
    "ban-ip": (False, "ban-ip {name}{reason}"),
    "pardon-ip": (False, "pardon-ip {name}"),
    "op": (False, "op {name}"),
    "deop": (False, "deop {name}"),
    "whitelist-add": (False, "whitelist add {name}"),
    "whitelist-remove": (False, "whitelist remove {name}"),
    "whitelist-on": (False, "whitelist on"),
    "whitelist-off": (False, "whitelist off"),
}
NO_TARGET = {"whitelist-on", "whitelist-off"}
IP_ACTIONS = {"ban-ip", "pardon-ip"}


class PlayerError(Exception):
    pass


def offline_uuid(name: str) -> str:
    """The UUID an ``online-mode=false`` server gives a player (Java's nameUUIDFromBytes)."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30   # version 3
    digest[8] = (digest[8] & 0x3F) | 0x80   # IETF variant
    return str(uuid.UUID(bytes=bytes(digest)))


def clean_reason(reason: str | None) -> str:
    # Commands are sent line by line, so a newline would start a second command.
    text = " ".join((reason or "").split())
    return text[:200]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


class Players:
    def __init__(self, server_dir: Path, http: HttpClient | None = None,
                 send: Callable[[str], None] | None = None):
        self.server_dir = server_dir
        self.http = http or HttpClient()
        #: sends a console command; None when the server is not running
        self.send = send

    # ---------------------------------------------------------------- files
    def _read(self, key: str) -> list[dict]:
        path = self.server_dir / FILES[key]
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, list) else []
        except (FileNotFoundError, ValueError):
            return []

    def _write(self, key: str, entries: list[dict]) -> None:
        self.server_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.server_dir, prefix=".players-")
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp, self.server_dir / FILES[key])

    @property
    def properties(self) -> dict[str, str]:
        return read_properties(self.server_dir / "server.properties")

    def summary(self, online: set[str] | None = None) -> dict:
        props = self.properties
        cache = sorted(self._read("usercache"), key=lambda e: e.get("expiresOn", ""), reverse=True)
        return {
            "running": self.send is not None,
            "online": sorted(online or [], key=str.lower),
            "ops": [{"name": e.get("name"), "uuid": e.get("uuid"), "level": e.get("level", 4)}
                    for e in self._read("ops")],
            "whitelist_enabled": props.get("white-list", "false") == "true",
            "whitelist": [{"name": e.get("name"), "uuid": e.get("uuid")} for e in self._read("whitelist")],
            "bans": [{"name": e.get("name"), "uuid": e.get("uuid"), "reason": e.get("reason", ""),
                      "created": e.get("created", ""), "source": e.get("source", "")} for e in self._read("bans")],
            "ip_bans": [{"ip": e.get("ip"), "reason": e.get("reason", ""), "created": e.get("created", "")}
                        for e in self._read("ip_bans")],
            "known": [{"name": e.get("name"), "uuid": e.get("uuid")} for e in cache[:100]],
            "online_mode": props.get("online-mode", "true") != "false",
        }

    # -------------------------------------------------------------- lookups
    def lookup(self, name: str) -> tuple[str, str]:
        """(uuid, correctly-cased name) for a player."""
        for entry in self._read("usercache"):
            if entry.get("name", "").lower() == name.lower():
                return entry["uuid"], entry["name"]
        if self.properties.get("online-mode", "true") == "false":
            return offline_uuid(name), name
        try:
            profile = self.http.get_json(f"{MOJANG_PROFILE}/{name}")
        except HttpError as e:
            if e.status in (204, 404):
                raise PlayerError(f"there is no Minecraft account named {name!r}") from e
            raise PlayerError(f"couldn't look up {name!r} with Mojang: {e}") from e
        except ValueError:  # an empty 204 response
            raise PlayerError(f"there is no Minecraft account named {name!r}") from None
        if not profile or "id" not in profile:
            raise PlayerError(f"there is no Minecraft account named {name!r}")
        return str(uuid.UUID(profile["id"])), profile.get("name", name)

    # -------------------------------------------------------------- actions
    def act(self, action: str, name: str = "", reason: str | None = None) -> str:
        if action not in ACTIONS:
            raise PlayerError(f"unknown action {action!r}")
        needs_running, template = ACTIONS[action]
        name = name.strip()
        if action in IP_ACTIONS:
            # ban-ip also accepts the name of an online player.
            if not (_is_ip(name) or (action == "ban-ip" and NAME_RE.match(name))):
                raise PlayerError(f"{name!r} is not a valid IP address or player name")
            if action == "ban-ip" and not _is_ip(name) and self.send is None:
                raise PlayerError("banning a player's IP by name needs the server running; enter the IP instead")
        elif action not in NO_TARGET and not NAME_RE.match(name):
            raise PlayerError(f"{name!r} is not a valid player name")
        if needs_running and self.send is None:
            raise PlayerError(f"{action} only works while the server is running")

        if self.send is not None:
            reason_text = clean_reason(reason)
            command = template.format(name=name, reason=f" {reason_text}" if reason_text else "")
            self.send(command)
            return f"sent: {command}"
        return self._offline(action, name, clean_reason(reason))

    def _offline(self, action: str, name: str, reason: str) -> str:
        if action in ("whitelist-on", "whitelist-off"):
            write_properties(self.server_dir / "server.properties",
                             {"white-list": "true" if action == "whitelist-on" else "false"})
            return f"whitelist {'enabled' if action == 'whitelist-on' else 'disabled'}"

        if action in IP_ACTIONS:
            entries = [e for e in self._read("ip_bans") if e.get("ip") != name]
            if action == "ban-ip":
                entries.append({"ip": name, "created": _now(), "source": "mcsm", "expires": "forever",
                                "reason": reason or "Banned by an operator."})
            self._write("ip_bans", entries)
            return f"{'banned' if action == 'ban-ip' else 'unbanned'} IP {name}"

        key = {"ban": "bans", "pardon": "bans", "op": "ops", "deop": "ops",
               "whitelist-add": "whitelist", "whitelist-remove": "whitelist"}[action]
        entries = self._read(key)
        removing = action in ("pardon", "deop", "whitelist-remove")
        if removing:
            kept = [e for e in entries if e.get("name", "").lower() != name.lower()]
            if len(kept) == len(entries):
                raise PlayerError(f"{name} is not {'banned' if key == 'bans' else 'an operator' if key == 'ops' else 'whitelisted'}")
            self._write(key, kept)
            return {"pardon": f"unbanned {name}", "deop": f"{name} is no longer an operator",
                    "whitelist-remove": f"removed {name} from the whitelist"}[action]

        player_uuid, proper = self.lookup(name)
        entries = [e for e in entries if e.get("uuid") != player_uuid]
        if action == "op":
            level = int(self.properties.get("op-permission-level", "4") or 4)
            entries.append({"uuid": player_uuid, "name": proper, "level": level, "bypassesPlayerLimit": False})
            message = f"made {proper} an operator (level {level})"
        elif action == "ban":
            entries.append({"uuid": player_uuid, "name": proper, "created": _now(), "source": "mcsm",
                            "expires": "forever", "reason": reason or "Banned by an operator."})
            message = f"banned {proper}"
        else:
            entries.append({"uuid": player_uuid, "name": proper})
            message = f"added {proper} to the whitelist"
        self._write(key, entries)
        return message + " (applies when the server starts)"
