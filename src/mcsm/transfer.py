"""Moving a server to another computer: export it to one .zip, import it there.

The export holds everything the server is: ``mcsm.toml``, ``mcsm.lock.json`` and the
server folder (worlds, mods, configs, server.properties, ops and whitelist, the
installed Minecraft and loader), plus backups if asked. Left out: logs, crash
reports, and mcsm's own ``.mcsm`` folder (the Java it downloaded suits only this
computer; the new one downloads its own on first start).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from . import __version__, config as configmod, lock as lockmod
from .config import ConfigError

log = logging.getLogger(__name__)

FORMAT = 1
MANIFEST = "mcsm-export.json"
SKIP_IN_SERVER = {"logs", "crash-reports", "debug", ".mcsm"}
SKIP_NAMES = {"session.lock"}  # the running world's lock; Minecraft makes a new one


class TransferError(ValueError):
    pass


def _walk(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        rel = Path(dirpath).relative_to(base)
        if rel == Path("."):
            dirnames[:] = [d for d in dirnames if d not in SKIP_IN_SERVER]
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for f in filenames:
            p = Path(dirpath) / f
            if f not in SKIP_NAMES and not p.is_symlink() and p.is_file():
                yield p, (rel / f).as_posix()


def export_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9 _'.()-]+", "", name).strip(" .")[:50] or "server"
    return f"{safe} {datetime.now().strftime('%Y-%m-%d %H%M')}.mcsm.zip"


def export(manager, dest: Path, include_backups: bool = False, say: Callable[[str], None] = log.info) -> Path:
    """Write the server to ``dest`` (a .zip). The server should be stopped."""
    cfg, lk = manager.config, manager.lock
    from .properties import read_properties
    props = read_properties(manager.server_dir / "server.properties")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    files = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=5, allowZip64=True) as z:
            z.writestr(MANIFEST, json.dumps({
                "format": FORMAT, "name": props.get("motd") or cfg.root.name, "folder": cfg.root.name,
                "minecraft": lk.minecraft, "loader": lk.loader, "mcsm": __version__,
                "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "backups": include_backups}, indent=2))
            z.write(cfg.path, configmod.CONFIG_NAME)
            if (cfg.root / lockmod.LOCK_NAME).exists():
                z.write(cfg.root / lockmod.LOCK_NAME, lockmod.LOCK_NAME)
            parts = [("server", cfg.server.dir)]
            if include_backups and cfg.backups.dir.is_dir():
                parts.append(("backups", cfg.backups.dir))
            last = time.monotonic()
            for prefix, base in parts:
                if not base.is_dir():
                    continue
                for path, rel in _walk(base):
                    if prefix == "server" and path.is_relative_to(cfg.backups.dir):
                        continue
                    z.write(path, f"{prefix}/{rel}")
                    files += 1
                    if time.monotonic() - last > 5:
                        say(f"exporting: {files} files so far")
                        last = time.monotonic()
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    say(f"exported {files} files to {dest} ({dest.stat().st_size / 1e6:.0f} MB)")
    return dest


def read_manifest(archive: Path) -> dict:
    try:
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            raw = z.read(MANIFEST) if MANIFEST in names and configmod.CONFIG_NAME in names else None
    except zipfile.BadZipFile as e:
        raise TransferError("that file isn't a .zip, or it's damaged") from e
    if raw is None:
        raise TransferError("that file isn't an mcsm server export (made with Export on a server's Settings page)")
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise TransferError("the export's details are damaged") from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise TransferError("that export was made by a newer mcsm; update mcsm first")
    return data


def _member_target(root: Path, name: str) -> Path | None:
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not parts or name.startswith(("/", "\\")) or any(p in ("..", "") for p in parts) or ":" in parts[0]:
        return None
    if parts[0] not in ("server", "backups") and name not in (configmod.CONFIG_NAME, lockmod.LOCK_NAME):
        return None
    target = root.joinpath(*parts)
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return target


def import_into(archive: Path, root: Path, say: Callable[[str], None] = log.info) -> dict:
    """Unpack an export into ``root`` (a new, empty folder)."""
    manifest = read_manifest(archive)
    if root.exists() and any(root.iterdir()):
        raise TransferError(f"{root} isn't empty")
    root.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                if member.is_dir() or member.filename == MANIFEST:
                    continue
                target = _member_target(root, member.filename)
                if target is None:
                    log.warning("skipped %s in the export (not a place mcsm writes to)", member.filename)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                count += 1
        path = root / configmod.CONFIG_NAME
        # The export's folders, wherever they were on the old computer, are now inside root.
        configmod.set_value(path, "server", "dir", '"server"')
        configmod.set_value(path, "backups", "dir", '"backups"')
        configmod.load(root)  # it must still make sense
        _fix_launch(root)
    except (ConfigError, OSError, zipfile.BadZipFile) as e:
        shutil.rmtree(root, ignore_errors=True)
        raise TransferError(f"couldn't import it: {e}") from e
    say(f"imported {manifest.get('name')} ({count} files)")
    return manifest


def _fix_launch(root: Path) -> None:
    """Forge/NeoForge start from win_args.txt on Windows and unix_args.txt elsewhere."""
    from .loaders.base import args_file_name
    lk = lockmod.load(root)
    want = args_file_name()
    changed = False
    for i, arg in enumerate(lk.launch):
        for other in ("win_args.txt", "unix_args.txt"):
            if arg.startswith("@") and arg.endswith(other) and other != want:
                lk.launch[i] = arg[: -len(other)] + want
                changed = True
    if changed:
        lockmod.save(root, lk, touch=False)
