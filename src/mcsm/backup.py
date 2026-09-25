"""Snapshots of the server directory, used for manual restores and automatic rollback."""

from __future__ import annotations

import logging
import re
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SUFFIX = ".tar.gz"


def create(server_dir: Path, backups_dir: Path, label: str, exclude: list[str]) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)
    dest = backups_dir / f"{stamp}-{safe}{SUFFIX}"
    tmp = dest.with_name(dest.name + ".part")
    excluded = set(exclude)

    def keep(info: tarfile.TarInfo):
        top = Path(info.name).parts[1] if len(Path(info.name).parts) > 1 else ""
        return None if top in excluded or info.name.endswith("session.lock") else info

    log.info("backing up %s -> %s", server_dir, dest)
    # Region files are already compressed, so a fast compression level is plenty.
    with tarfile.open(tmp, "w:gz", compresslevel=1) as tar:
        tar.add(server_dir, arcname="server", filter=keep)
    tmp.rename(dest)
    return dest


def list_backups(backups_dir: Path) -> list[Path]:
    if not backups_dir.exists():
        return []
    return sorted(backups_dir.glob(f"*{SUFFIX}"))


def prune(backups_dir: Path, keep: int) -> list[Path]:
    backups = list_backups(backups_dir)
    removed = backups[:-keep] if keep > 0 else []
    for path in removed:
        path.unlink()
    return removed


def restore(archive: Path, server_dir: Path) -> None:
    """Replace ``server_dir`` with the archive's contents.

    The current directory is kept as ``<dir>.replaced`` until the restore succeeds.
    """
    staging = server_dir.with_name(server_dir.name + ".restoring")
    aside = server_dir.with_name(server_dir.name + ".replaced")
    for p in (staging, aside):
        if p.exists():
            shutil.rmtree(p)
    staging.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(staging, filter="data")
    restored = staging / "server"
    if not restored.is_dir():
        shutil.rmtree(staging)
        raise ValueError(f"{archive} is not an mcsm backup")
    if server_dir.exists():
        server_dir.rename(aside)
    restored.rename(server_dir)
    shutil.rmtree(staging)
    if aside.exists():
        shutil.rmtree(aside)
