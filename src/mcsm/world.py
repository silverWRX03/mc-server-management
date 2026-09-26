"""Bringing an existing Minecraft world to a server.

A world can come from a .zip (a zipped world folder, a server's world, or a
download) or from the singleplayer worlds on this computer: the Minecraft Launcher's
``saves`` folder, and the instances of Prism Launcher, the Modrinth App and
CurseForge. The world goes into the server's world folder (``level-name``);
a server's separate ``world_nether``/``world_the_end`` folders (Paper, Spigot) are
folded back into the vanilla layout.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import logging
import os
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

from . import nbt

log = logging.getLogger(__name__)

MAX_WORLD = 64 << 30   # uncompressed; refuses zip bombs
SKIP = {"session.lock"}


class WorldError(ValueError):
    pass


# ----------------------------------------------------------------- reading
def level_info(level_dat: bytes) -> dict:
    """The world's name and the Minecraft version it was last played on."""
    try:
        data = nbt.loads(gzip.decompress(level_dat)).get("Data", {})
    except (OSError, EOFError, nbt.NBTError):
        return {}
    version = data.get("Version") if isinstance(data.get("Version"), dict) else {}
    return {"name": data.get("LevelName") if isinstance(data.get("LevelName"), str) else None,
            "version": version.get("Name") if isinstance(version.get("Name"), str) else None,
            "hardcore": bool(getattr(data.get("hardcore"), "value", 0))}


def _world_prefix(names: list[str]) -> str:
    """The folder in a zip that holds level.dat (the shallowest one)."""
    found = sorted((n for n in names if n.replace("\\", "/").split("/")[-1] == "level.dat"),
                   key=lambda n: n.count("/"))
    if not found:
        raise WorldError("there's no Minecraft world in that file (no level.dat); zip the world's folder and try again")
    return found[0][: -len("level.dat")].replace("\\", "/")


def _safe_rel(rel: str) -> PurePosixPath | None:
    p = PurePosixPath(rel.replace("\\", "/"))
    if not p.parts or rel.startswith(("/", "\\")) or any(x in ("..", "") for x in p.parts) or ":" in p.parts[0]:
        return None
    return p


def _extract(z: zipfile.ZipFile, prefix: str, dest: Path, sub: str = "") -> int:
    count = 0
    for member in z.infolist():
        name = member.filename.replace("\\", "/")
        if member.is_dir() or not name.startswith(prefix):
            continue
        rel = _safe_rel(name[len(prefix):])
        if rel is None or rel.name in SKIP:
            continue
        target = dest.joinpath(*PurePosixPath(sub).parts, *rel.parts) if sub else dest.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        count += 1
    return count


def inspect_zip(archive: Path) -> dict:
    try:
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            prefix = _world_prefix(names)
            info = level_info(z.read(prefix + "level.dat"))
            size = sum(i.file_size for i in z.infolist())
    except zipfile.BadZipFile as e:
        raise WorldError("that file isn't a .zip, or it's damaged") from e
    if size > MAX_WORLD:
        raise WorldError("that world is too big to import")
    return {**info, "size": size}


# --------------------------------------------------------------- installing
def install(source: Path, dest: Path) -> dict:
    """Put a world (a .zip or a world folder) at ``dest``, which must not exist yet."""
    if dest.exists():
        raise WorldError(f"{dest} already exists")
    tmp = dest.with_name(f".{dest.name}.importing")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        if source.is_dir():
            if not (source / "level.dat").is_file():
                raise WorldError("that folder isn't a Minecraft world (no level.dat)")
            shutil.copytree(source, tmp, ignore=shutil.ignore_patterns(*SKIP))
            info = level_info((source / "level.dat").read_bytes())
        else:
            info = inspect_zip(source)
            with zipfile.ZipFile(source) as z:
                names = [n.replace("\\", "/") for n in z.namelist()]
                prefix = _world_prefix(names)
                _extract(z, prefix, tmp)
                # A Paper/Spigot server keeps the Nether and the End next to the world.
                base = prefix.rstrip("/")
                for suffix, dim in (("_nether", "DIM-1"), ("_the_end", "DIM1")):
                    side = f"{base}{suffix}/{dim}/" if base else None
                    if side and any(n.startswith(side) for n in names) and not (tmp / dim).exists():
                        _extract(z, side, tmp, dim)
        os.replace(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log.info("imported the world %s%s", info.get("name") or dest.name,
             f" (last played on Minecraft {info['version']})" if info.get("version") else "")
    return info


def replace(source: Path, dest: Path) -> dict:
    """Swap the world at ``dest`` for another (the caller makes a backup first)."""
    old = dest.with_name(f".{dest.name}.replaced")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        os.replace(dest, old)
    try:
        info = install(source, dest)
    except Exception:
        if old.exists() and not dest.exists():
            os.replace(old, dest)  # put the old world back
        raise
    shutil.rmtree(old, ignore_errors=True)
    return info


# ------------------------------------------------------ singleplayer worlds
def _launcher_saves() -> list[tuple[str, Path]]:
    """(launcher, saves folder) pairs that exist on this computer."""
    from . import join, launchers
    out: list[tuple[str, Path]] = [("Minecraft Launcher", join.minecraft_dir() / "saves")]
    for d in launchers.prism_dirs():
        if (d / "instances").is_dir():
            for inst in sorted((d / "instances").iterdir()):
                for game in ("minecraft", ".minecraft"):
                    out.append((f"Prism: {inst.name}", inst / game / "saves"))
    for d in launchers.modrinth_dirs():
        if (d / "profiles").is_dir():
            for prof in sorted((d / "profiles").iterdir()):
                out.append((f"Modrinth: {prof.name}", prof / "saves"))
    for d in launchers.curseforge_dirs():
        if (d / "Instances").is_dir():
            for inst in sorted((d / "Instances").iterdir()):
                out.append((f"CurseForge: {inst.name}", inst / "saves"))
    if os.name != "nt" and sys.platform != "darwin":
        flatpak = Path.home() / ".var/app/com.mojang.Minecraft/.minecraft/saves"
        out.append(("Minecraft Launcher (Flatpak)", flatpak))
    return [(name, p) for name, p in out if p.is_dir()]


def save_id(path: Path) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:16]


def list_saves(sources: list[tuple[str, Path]] | None = None, limit: int = 200) -> list[dict]:
    """Singleplayer worlds on this computer, newest first."""
    worlds = []
    for launcher, saves in sources if sources is not None else _launcher_saves():
        try:
            entries = list(saves.iterdir())
        except OSError:
            continue
        for w in entries:
            level = w / "level.dat"
            if not level.is_file():
                continue
            try:
                info = level_info(level.read_bytes())
                icon = w / "icon.png"
                worlds.append({
                    "id": save_id(w), "folder": w.name, "name": info.get("name") or w.name,
                    "version": info.get("version"), "hardcore": info.get("hardcore", False), "launcher": launcher,
                    "played": level.stat().st_mtime, "path": str(w),
                    "icon": "data:image/png;base64," + base64.b64encode(icon.read_bytes()).decode()
                            if icon.is_file() and icon.stat().st_size < 64 * 1024 else None})
            except OSError:
                continue
    worlds.sort(key=lambda x: x["played"], reverse=True)
    return worlds[:limit]


def find_save(sid: str, sources: list[tuple[str, Path]] | None = None) -> Path:
    for w in list_saves(sources, limit=10_000):
        if w["id"] == sid:
            return Path(w["path"])
    raise WorldError("that world isn't there any more")
