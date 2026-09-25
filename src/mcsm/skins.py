"""Player skins for the dashboard's head icons.

mcsm fetches the skin itself (Mojang's session and texture servers) and serves it
from the control panel, so the browser never loads images from a third party. The
page cuts the face out of the skin. Offline-mode servers have no skins; the page
shows a lettered tile instead.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from pathlib import Path

from .http import HttpClient, HttpError
from .players import MOJANG_PROFILE, NAME_RE

SESSION_PROFILE = "https://sessionserver.mojang.com/session/minecraft/profile"
TEXTURE_HOST = re.compile(r"^https?://textures\.minecraft\.net/texture/[0-9a-f]+$")
KEEP = 86400          # re-fetch a skin after a day
RETRY_FAILED = 600    # don't ask Mojang again for 10 minutes after a miss
MAX_SKIN = 64 * 1024  # skins are 64x64 PNGs of a few KB


class SkinError(Exception):
    pass


class Skins:
    def __init__(self, cache_dir: Path, server_dir: Path, http: HttpClient | None = None):
        self.cache_dir = cache_dir
        self.server_dir = server_dir
        # One quick try: a missing head icon isn't worth waiting out Mojang's rate limits.
        self.http = http or HttpClient(retries=1, timeout=10, cache_ttl=0, rate_limit_retries=0)
        self._misses: dict[str, float] = {}
        self._lock = threading.Lock()

    def _uuid(self, name: str) -> str:
        try:
            for entry in json.loads((self.server_dir / "usercache.json").read_text()):
                if str(entry.get("name", "")).lower() == name.lower():
                    return str(entry["uuid"]).replace("-", "")
        except (OSError, ValueError, KeyError, TypeError):
            pass
        from .properties import read_properties
        if read_properties(self.server_dir / "server.properties").get("online-mode", "true") == "false":
            raise SkinError("offline-mode servers have no skins")
        return str(self.http.get_json(f"{MOJANG_PROFILE}/{name}")["id"]).replace("-", "")

    def png(self, name: str) -> bytes:
        """The player's skin PNG (cached on disk)."""
        if not NAME_RE.match(name):
            raise SkinError("not a player name")
        key = name.lower()
        path = self.cache_dir / f"{key}.png"
        if path.exists() and time.time() - path.stat().st_mtime < KEEP:
            return path.read_bytes()
        with self._lock:
            if time.monotonic() - self._misses.get(key, -RETRY_FAILED) < RETRY_FAILED:
                raise SkinError("no skin")
        try:
            uuid = self._uuid(name)
            if not re.fullmatch(r"[0-9a-f]{32}", uuid):
                raise SkinError("unexpected profile id")
            profile = self.http.get_json(f"{SESSION_PROFILE}/{uuid}")
            textures = next(p["value"] for p in profile.get("properties", []) if p.get("name") == "textures")
            url = json.loads(base64.b64decode(textures))["textures"]["SKIN"]["url"]
            if not TEXTURE_HOST.match(url):
                raise SkinError("unexpected texture address")
            tmp = self.http.download(url.replace("http://", "https://", 1), self.cache_dir / f".{key}.download")
            data = tmp.read_bytes()
            tmp.unlink(missing_ok=True)
            if len(data) > MAX_SKIN or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise SkinError("not a skin image")
        except (HttpError, OSError, ValueError, KeyError, TypeError, StopIteration, SkinError) as e:
            with self._lock:
                self._misses[key] = time.monotonic()
            if path.exists():
                return path.read_bytes()  # an old copy beats none
            raise SkinError(str(e) or "no skin") from e
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data
