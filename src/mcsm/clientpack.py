"""What a friend's Minecraft needs to join a server: the "client pack".

The pack is a small JSON description (Minecraft and loader version, the server's
address, and a download link and hash for every mod a player needs). The friend's
copy of mcsm (`mcsm join`, see join.py) fetches it from the share server and sets up
an installation in their own Minecraft Launcher. Nothing is redistributed: every mod
is downloaded by the player straight from Modrinth or CurseForge.

Which mods go in: every mod the server runs that also runs on clients (Modrinth's
``client_side`` isn't "unsupported"; CurseForge doesn't say, so those are included),
plus client-only extras the server's admin picked (a minimap, JEI, Sodium ...) and
their required dependencies.
"""

from __future__ import annotations

import base64
import logging
import secrets
import time
from urllib.parse import urlparse

from .config import ModSpec
from .mods.base import ModError, ModFile, Unavailable
from .mods.modrinth import ModrinthProvider

log = logging.getLogger(__name__)

FORMAT = 1
# Where a pack may send players' downloads (HTTPS only). Anything else is refused on
# both ends, so a tampered pack can't point players at arbitrary files.
DOWNLOAD_HOSTS = ("cdn.modrinth.com", "edge.forgecdn.net", "mediafilez.forgecdn.net")
CACHE_SECONDS = 300


def new_token() -> str:
    return secrets.token_urlsafe(18)


def allowed_url(url: str) -> bool:
    u = urlparse(url)
    return u.scheme == "https" and u.hostname in DOWNLOAD_HOSTS


def _entry(m: ModFile, side: str) -> dict:
    return {"name": m.name, "filename": m.filename, "url": m.url, "sha1": m.sha1, "sha512": m.sha512,
            "project": m.key, "side": side}


class PackBuilder:
    def __init__(self, manager):
        self.m = manager
        self._cache: tuple[tuple, float, dict] | None = None

    def build(self, address: str) -> dict:
        m = self.m
        lk, cfg = m.lock, m.config
        if not lk.installed:
            raise ModError("the server isn't installed yet")
        key = (lk.updated_at, lk.minecraft, tuple(cfg.client.mods), cfg.client.memory_gb, address,
               cfg.server.dir)
        if self._cache and self._cache[0] == key and time.monotonic() - self._cache[1] < CACHE_SECONDS:
            return self._cache[2]
        pack = self._build(address)
        self._cache = (key, time.monotonic(), pack)
        return pack

    def _build(self, address: str) -> dict:
        m = self.m
        lk, cfg = m.lock, m.config
        modrinth = m.providers.get("modrinth") or ModrinthProvider(m.http)
        loaders = m.loader.mod_loaders
        mods: list[dict] = []
        manual: list[dict] = []
        skipped: list[dict] = []
        included: set[str] = set()

        # The server's own mods, unless they only run on servers.
        sides = {}
        ids = [x.project_id for x in lk.mods if x.source == "modrinth"]
        if ids:
            try:
                sides = {pid: p.get("client_side", "unknown") for pid, p in modrinth.projects(ids).items()}
            except Exception as e:  # can't tell: include them all (a spare server mod is harmless)
                log.warning("couldn't look up which mods players need (%s); including all of them", e)
        for x in lk.mods:
            if x.source == "modrinth" and sides.get(x.project_id) == "unsupported":
                continue
            included.add(x.key)
            if x.manual or not allowed_url(x.url):
                manual.append({"name": x.name, "filename": x.filename, "url": x.manual_url or x.url, "sha1": x.sha1})
            else:
                mods.append(_entry(x, "both"))

        # Extras only players need, with their required dependencies.
        todo = [ModSpec("modrinth", slug) for slug in cfg.client.mods]
        while todo and loaders:
            spec = todo.pop(0)
            try:
                f = modrinth.resolve(spec, lk.minecraft, loaders, cfg.updates.mod_channel, side="client")
            except (Unavailable, ModError) as e:
                if spec.dependency_of is None:
                    skipped.append({"name": spec.id, "reason": str(e)})
                continue
            if f.key in included:
                continue
            included.add(f.key)
            (mods if allowed_url(f.url) else manual).append(_entry(f, "client"))
            todo += [ModSpec("modrinth", dep, dependency_of=f.name) for dep in f.dependencies
                     if f"modrinth:{dep}" not in included]

        from .properties import read_properties
        props = read_properties(m.server_dir / "server.properties")
        icon = m.server_dir / "server-icon.png"
        return {
            "format": FORMAT,
            "name": props.get("motd") or cfg.root.name,
            "minecraft": lk.minecraft,
            "loader": lk.loader,
            "loader_version": lk.loader_version,
            "java_major": lk.java_major,
            "address": address,
            "memory_gb": cfg.client.memory_gb,
            "mods": mods,
            "manual": manual,
            "skipped": skipped,
            "icon": ("data:image/png;base64," + base64.b64encode(icon.read_bytes()).decode())
                    if icon.is_file() and icon.stat().st_size < 64 * 1024 else None,
            "updated": lk.updated_at,
        }
