"""Mods' config files, for viewing and editing in the web UI.

Config files live in the server's ``config/`` folder (Fabric, Quilt, NeoForge, Forge,
Paper's own settings), ``defaultconfigs/`` (copied into new worlds), ``<world>/serverconfig/``
(Forge and NeoForge per-world settings) and ``plugins/<plugin>/`` (Paper plugins). Each is
matched to the mod it belongs to by the ids declared inside the jars (fabric.mod.json,
quilt.mod.json, META-INF/mods.toml, META-INF/neoforge.mods.toml, plugin.yml,
paper-plugin.yml).

Only text config formats inside those folders can be read or written, and every save
keeps the previous version in ``.mcsm/config-backups/``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath

from .properties import read_properties

EXTENSIONS = {".toml": "toml", ".json": "json", ".json5": "json", ".jsonc": "json", ".mcmeta": "json",
              ".yml": "yaml", ".yaml": "yaml", ".properties": "properties", ".cfg": "cfg", ".conf": "cfg",
              ".ini": "ini", ".snbt": "snbt", ".txt": "text"}
MAX_SIZE = 1 << 20
MAX_FILES = 2000
KEEP_BACKUPS = 10
_SUFFIXES = re.compile(r"[-_.](common|server|client|config|settings|mixins?)$")


class ConfigFileError(ValueError):
    pass


def roots(server_dir: Path) -> list[Path]:
    level = read_properties(server_dir / "server.properties").get("level-name") or "world"
    return [server_dir / "config", server_dir / "defaultconfigs", server_dir / level / "serverconfig",
            server_dir / "plugins"]


def jars(server_dir: Path) -> list[Path]:
    """Mod jars in mods/ and plugin jars in plugins/."""
    out = []
    for folder in ("mods", "plugins"):
        if (server_dir / folder).is_dir():
            out += sorted((server_dir / folder).glob("*.jar"))
    return out


# ------------------------------------------------------------ which mod is it
_jar_cache: dict[tuple[str, float], list[tuple[str, str]]] = {}


def jar_mods(jar: Path) -> list[tuple[str, str]]:
    """(mod id, display name) pairs a jar declares."""
    try:
        key = (str(jar), jar.stat().st_mtime)
    except OSError:
        return []
    if key in _jar_cache:
        return _jar_cache[key]
    found: list[tuple[str, str]] = []
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
            if "fabric.mod.json" in names:
                data = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"), strict=False)
                if isinstance(data, dict) and isinstance(data.get("id"), str):
                    found.append((data["id"], str(data.get("name") or data["id"])))
            if "quilt.mod.json" in names:
                data = json.loads(z.read("quilt.mod.json").decode("utf-8", "replace"), strict=False)
                loader = data.get("quilt_loader", {}) if isinstance(data, dict) else {}
                if isinstance(loader.get("id"), str):
                    meta = loader.get("metadata") or {}
                    found.append((loader["id"], str(meta.get("name") or loader["id"])))
            for toml in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if toml in names:
                    text = z.read(toml).decode("utf-8", "replace")
                    for block in re.split(r"^\s*\[\[mods\]\]\s*$", text, flags=re.M)[1:]:
                        mid = re.search(r'^\s*modId\s*=\s*"([^"]+)"', block, re.M)
                        name = re.search(r'^\s*displayName\s*=\s*"([^"]+)"', block, re.M)
                        if mid:
                            found.append((mid.group(1), name.group(1) if name else mid.group(1)))
                    break
            for yml in ("paper-plugin.yml", "plugin.yml"):
                if yml in names:
                    name = re.search(r"^name:\s*['\"]?([\w.-]+)", z.read(yml).decode("utf-8", "replace"), re.M)
                    if name:
                        found.append((name.group(1), name.group(1)))
                    break
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        pass
    _jar_cache[key] = found
    return found


def _stem(rel: str) -> str:
    """The part of a config path that names its mod: "create-server.toml" -> "create"."""
    p = PurePosixPath(rel)
    first = p.parts[0] if len(p.parts) > 1 else p.name.split(".")[0]
    first = first.lower()
    while True:
        cut = _SUFFIXES.sub("", first)
        if cut == first:
            return first
        first = cut


def list_files(server_dir: Path) -> list[str]:
    """Editable config files, as paths relative to the server folder."""
    out = []
    for root in roots(server_dir):
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for f in sorted(filenames):
                p = Path(dirpath) / f
                if p.suffix.lower() in EXTENSIONS and not p.is_symlink():
                    out.append(p.relative_to(server_dir).as_posix())
                    if len(out) >= MAX_FILES:
                        return out
    return out


def grouped(server_dir: Path, lock_mods) -> dict:
    """Config files per mod (by mod id), and the ones no installed mod claims."""
    mods = []
    by_id: dict[str, dict] = {}
    names = {m.filename: m.name for m in lock_mods}
    for jar in jars(server_dir):
        for mod_id, display in jar_mods(jar):
            if mod_id in ("minecraft", "java", "fabricloader") or mod_id in by_id:
                continue
            entry = {"mod_id": mod_id, "name": names.get(jar.name) or display, "jar": jar.name, "files": []}
            by_id[mod_id] = entry
            mods.append(entry)
    ids = sorted(by_id, key=len, reverse=True)  # "create_sa" before "create"
    other = []
    for rel in list_files(server_dir):
        within = rel.split("/", 1)[1] if "/" in rel else rel  # without config/ (or plugins/)
        if rel.count("/") >= 2 and "/serverconfig/" in "/" + rel:
            within = rel.split("/serverconfig/", 1)[1]
        stem = _stem(within)
        norm = stem.replace("-", "").replace("_", "")
        match = next((i for i in ids if stem == i.lower() or norm == i.lower().replace("_", "").replace("-", "")
                      or stem.startswith(i.lower() + "-") or stem.startswith(i.lower() + "_")), None)
        (by_id[match]["files"] if match else other).append(rel)
    return {"mods": [m for m in mods if m["files"]], "other": other}


# --------------------------------------------------------------- read/write
def resolve(server_dir: Path, rel: str) -> Path:
    """The file for a relative path, if it's an editable config file inside the config folders."""
    p = PurePosixPath(rel.replace("\\", "/"))
    if not rel or rel.startswith(("/", "\\")) or any(x in ("..", "") for x in p.parts) or ":" in rel:
        raise ConfigFileError("that isn't a config file")
    path = server_dir.joinpath(*p.parts)
    if path.suffix.lower() not in EXTENSIONS:
        raise ConfigFileError("only text config files can be edited here")
    real = path.resolve()
    if not any(real.is_relative_to(r.resolve()) for r in roots(server_dir)):
        raise ConfigFileError("that file isn't in a config folder")
    return path


def read(server_dir: Path, rel: str) -> dict:
    path = resolve(server_dir, rel)
    if not path.is_file():
        raise ConfigFileError("that file doesn't exist (any more)")
    if path.stat().st_size > MAX_SIZE:
        raise ConfigFileError("that file is too big to edit here")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ConfigFileError("that file isn't text") from e
    return {"path": rel, "text": text, "format": EXTENSIONS[path.suffix.lower()], "size": len(raw),
            "modified": path.stat().st_mtime}


def write(server_dir: Path, backups: Path, rel: str, text: str, expected_modified: float | None = None) -> dict:
    path = resolve(server_dir, rel)
    if len(text.encode("utf-8")) > MAX_SIZE:
        raise ConfigFileError("that's too big")
    if not path.is_file():
        raise ConfigFileError("that file doesn't exist (any more)")
    if expected_modified is not None and abs(path.stat().st_mtime - expected_modified) > 0.001:
        raise ConfigFileError("the file changed on disk since you opened it (maybe the mod rewrote it); "
                              "reload it and make your change again")
    if path.suffix.lower() == ".json":
        try:
            json.loads(text)
        except ValueError as e:
            raise ConfigFileError(f"that isn't valid JSON: {e}") from None
    # keep the previous version
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backups / (rel.replace("/", "__") + f".{stamp}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    olds = sorted(backups.glob(rel.replace("/", "__") + ".*"))
    for old in olds[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    # keep the file's own line endings
    original = path.read_bytes()
    if b"\r\n" in original and "\r\n" not in text:
        text = text.replace("\n", "\r\n")
    tmp = path.with_name(f".{path.name}.mcsm-tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)
    return {"ok": True, "modified": path.stat().st_mtime, "backup": target.name}
