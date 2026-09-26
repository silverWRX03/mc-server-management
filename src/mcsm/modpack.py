"""Setting a server up from a Modrinth modpack (.mrpack).

A modpack fixes the Minecraft version and mod loader and lists its mods. mcsm reads
the pack's ``modrinth.index.json`` and:

* sets the server's loader and Minecraft version, and keeps it on that version
  (strategy "mods-only": the pack's mods are made to work together on it);
* adds every mod hosted on Modrinth as a normal mcsm mod (kept up to date);
* downloads other files the server needs (checked against their hashes);
* copies the pack's ``overrides/`` and ``server-overrides/`` (configs, scripts...)
  into the server folder.

Files marked client-only (``env.server == "unsupported"``) are skipped.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from . import config as configmod
from .config import ModSpec
from .http import HttpClient
from .mods.base import ModError
from .mods.modrinth import API as MODRINTH

log = logging.getLogger(__name__)

LOADER_KEYS = {"fabric-loader": "fabric", "quilt-loader": "quilt", "neoforge": "neoforge", "forge": "forge"}
MODRINTH_FILE = re.compile(r"^https://cdn\.modrinth\.com/data/([A-Za-z0-9]{8})/versions/[A-Za-z0-9]{8}/[^/]+$")
DOWNLOAD_HOSTS = ("https://cdn.modrinth.com/", "https://github.com/", "https://raw.githubusercontent.com/",
                  "https://gitlab.com/")
MAX_PACK = 512 << 20


class ModpackError(ModError):
    pass


def _safe_target(base: Path, relative: str) -> Path | None:
    """``base / relative`` if it stays inside ``base`` (no absolute paths or ``..``)."""
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    if not parts or any(p in ("..", "") for p in parts) or relative.startswith(("/", "\\")) or ":" in parts[0]:
        return None
    target = base.joinpath(*parts)
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        return None
    return target


def version_info(http: HttpClient, version_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9]{8}", version_id or ""):
        raise ModpackError("that isn't a Modrinth modpack version")
    v = http.get_json(f"{MODRINTH}/version/{version_id}")
    files = [f for f in v.get("files", []) if f.get("filename", "").endswith(".mrpack")]
    if not files:
        raise ModpackError(f"{v.get('name', 'that version')} has no .mrpack file")
    f = next((x for x in files if x.get("primary")), files[0])
    return {"id": v["id"], "project_id": v["project_id"], "name": v.get("name") or v.get("version_number", ""),
            "url": f["url"], "sha1": f.get("hashes", {}).get("sha1"), "sha512": f.get("hashes", {}).get("sha512"),
            "minecraft": v.get("game_versions", []), "loaders": v.get("loaders", [])}


def read_index(pack: Path) -> tuple[dict, zipfile.ZipFile]:
    z = zipfile.ZipFile(pack)
    try:
        index = json.loads(z.read("modrinth.index.json"))
    except (KeyError, ValueError) as e:
        z.close()
        raise ModpackError("that file isn't a Modrinth modpack (no modrinth.index.json)") from e
    if index.get("game") != "minecraft":
        z.close()
        raise ModpackError("that modpack isn't for Minecraft: Java Edition")
    return index, z


def apply(root: Path, version_id: str, http: HttpClient) -> dict:
    """Install a modpack version into the server at ``root`` (whose mcsm.toml exists)."""
    info = version_info(http, version_id)
    with tempfile.TemporaryDirectory() as tmp:
        pack = http.download(info["url"], Path(tmp) / "pack.mrpack", sha1=info["sha1"], sha512=info["sha512"])
        if pack.stat().st_size > MAX_PACK:
            raise ModpackError("that modpack is too big")
        return apply_file(root, pack, http, name=info["name"])


def apply_file(root: Path, pack: Path, http: HttpClient, name: str = "") -> dict:
    index, z = read_index(pack)
    with z:
        deps = index.get("dependencies", {})
        minecraft = str(deps.get("minecraft", ""))
        if not re.fullmatch(r"\d+(\.\d+){1,3}(-[A-Za-z0-9.]+)?", minecraft):
            raise ModpackError("the modpack doesn't say which Minecraft version it's for")
        loaders = [LOADER_KEYS[k] for k in deps if k in LOADER_KEYS]
        loader = loaders[0] if loaders else "vanilla"
        cfg_path = root / configmod.CONFIG_NAME
        configmod.set_value(cfg_path, "server", "loader", json.dumps(loader))
        configmod.set_value(cfg_path, "server", "minecraft", json.dumps(minecraft))
        configmod.set_value(cfg_path, "updates", "strategy", '"mods-only"')
        cfg = configmod.load(root)
        server_dir = cfg.server.dir
        server_dir.mkdir(parents=True, exist_ok=True)
        listed = {s.id for s in cfg.mods}

        added, local, skipped = [], [], 0
        for f in index.get("files", []):
            if (f.get("env") or {}).get("server") == "unsupported":
                skipped += 1
                continue
            path = str(f.get("path", ""))
            target = _safe_target(server_dir, path)
            if target is None:
                continue
            urls = [u for u in f.get("downloads", []) if isinstance(u, str)]
            m = next((MODRINTH_FILE.match(u) for u in urls if MODRINTH_FILE.match(u)), None)
            if m and path.startswith("mods/") and path.endswith(".jar"):
                pid = m.group(1)
                if pid not in listed:
                    configmod.append_mod(cfg_path, ModSpec("modrinth", pid, required=True))
                    listed.add(pid)
                    added.append(pid)
                continue
            url = next((u for u in urls if u.startswith(DOWNLOAD_HOSTS)), None)
            hashes = f.get("hashes") or {}
            if not url or not (hashes.get("sha1") or hashes.get("sha512")):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            http.download(url, target, sha1=hashes.get("sha1"), sha512=hashes.get("sha512"))
            local.append(path)

        copied = 0
        for prefix in ("overrides/", "server-overrides/"):  # the server's own overrides win
            for member in z.infolist():
                if member.filename.startswith(prefix) and not member.is_dir():
                    target = _safe_target(server_dir, member.filename[len(prefix):])
                    if target is None:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(member) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    copied += 1
    title = name or index.get("name", "modpack")
    log.info("applied %s: Minecraft %s, %s, %d mods from Modrinth, %d other files, %d config files",
             title, minecraft, loader, len(added), len(local), copied)
    return {"name": title, "minecraft": minecraft, "loader": loader, "mods": len(added), "files": len(local),
            "overrides": copied, "client_only": skipped}
