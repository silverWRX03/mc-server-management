"""The Minecraft launchers a friend can add a server's instance to.

* **Minecraft Launcher** (the official one): an installation with its own folder,
  set up directly in the launcher's files (join.Joiner).
* **Prism Launcher** (and MultiMC-style launchers): an instance folder, with the
  server's address set to join on launch. Prism downloads Minecraft and the loader.
* **Modrinth App**: a ``.mrpack`` modpack file that the app imports when opened.
* **CurseForge app**: a CurseForge-format modpack zip, imported from
  "Create Custom Profile → Import".

Every mod is downloaded by this computer from Modrinth or CurseForge and checked
against its hash; the files made here only go into this person's own launchers.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import nbt, opener
from .http import HashMismatch, sha1_file

if TYPE_CHECKING:
    from .join import Joiner

KEYS = ("minecraft", "prism", "modrinth", "curseforge")
LABELS = {"minecraft": "Minecraft Launcher", "prism": "Prism Launcher", "modrinth": "Modrinth App",
          "curseforge": "CurseForge"}
MRPACK_HOSTS = ("https://cdn.modrinth.com/",)  # what the Modrinth App downloads by itself


@dataclass
class Found:
    key: str
    label: str
    found: bool
    where: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "found": self.found, "where": self.where, "note": self.note}


def _home_dirs(win: list[str], mac: list[str], linux: list[str]) -> list[Path]:
    """Candidate folders; entries starting with "%NAME%" use that environment variable."""
    if os.name == "nt":
        names = win
    elif sys.platform == "darwin":
        names = mac
    else:
        names = linux
    out = []
    for n in names:
        if n.startswith("%"):
            var, _, rest = n[1:].partition("%")
            base = os.environ.get(var)
            if not base:
                continue
            out.append(Path(base) / rest.lstrip("/\\"))
        else:
            out.append(Path.home() / n)
    return out


def prism_dirs() -> list[Path]:
    return _home_dirs(["%APPDATA%/PrismLauncher"],
                      ["Library/Application Support/PrismLauncher"],
                      [".local/share/PrismLauncher", ".var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher"])


def modrinth_dirs() -> list[Path]:
    return _home_dirs(["%APPDATA%/ModrinthApp", "%APPDATA%/com.modrinth.theseus"],
                      ["Library/Application Support/ModrinthApp", "Library/Application Support/com.modrinth.theseus"],
                      [".local/share/ModrinthApp", ".local/share/com.modrinth.theseus"])


def curseforge_dirs() -> list[Path]:
    return _home_dirs(["curseforge/minecraft", "%USERPROFILE%/curseforge/minecraft"],
                      ["Documents/curseforge/minecraft"], ["curseforge/minecraft"])


def detect(mc_dir: Path, prism_dir: Path | None = None) -> list[Found]:
    """Which launchers this computer seems to have (all can be picked either way)."""
    from .join import PROFILE_FILES
    prism = [prism_dir] if prism_dir else [d for d in prism_dirs() if (d / "instances").is_dir() or (d / "prismlauncher.cfg").exists()]
    mr = [d for d in modrinth_dirs() if d.is_dir()]
    cf = [d for d in curseforge_dirs() if d.is_dir()]
    official = any((mc_dir / f).exists() for f in PROFILE_FILES)
    return [
        Found("minecraft", LABELS["minecraft"], official, str(mc_dir),
              "" if official else "Install it from minecraft.net, open it and sign in once."),
        Found("prism", LABELS["prism"], bool(prism), str(prism[0]) if prism else "",
              "" if prism else "Not found. Install it from prismlauncher.org and open it once."),
        Found("modrinth", LABELS["modrinth"], bool(mr), str(mr[0]) if mr else "",
              "Opens a modpack file that the Modrinth App imports." if mr else
              "You get a .mrpack file to import once it's installed (modrinth.com/app)."),
        Found("curseforge", LABELS["curseforge"], bool(cf), str(cf[0]) if cf else "",
              "You get a modpack zip to import (Create Custom Profile → Import)."),
    ]


# ------------------------------------------------------------------ helpers
def fetch_mods(joiner: "Joiner", pack: dict, dest: Path) -> list[tuple[dict, Path]]:
    """Download every mod the pack lists into ``dest``, checking hashes."""
    from .join import JoinError
    from .join import folder_of
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for m in pack.get("mods", []):
        target = dest / folder_of(m) / m["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if not (target.exists() and m.get("sha1") and sha1_file(target) == m["sha1"]):
            joiner.say(f"  downloading {m['name']}")
            try:
                joiner.http.download(m["url"], target, sha1=m.get("sha1"), sha512=m.get("sha512"))
            except HashMismatch as e:
                raise JoinError(f"{m['name']} didn't match its checksum, so it wasn't installed") from e
        out.append((m, target))
    return out


def servers_dat(pack: dict) -> bytes:
    return nbt.add_server(None, pack["name"], pack["address"])


def _safe_name(name: str) -> str:
    keep = "".join(c for c in name if c.isalnum() or c in " -_'.()").strip(" .")
    return keep[:60] or "Minecraft server"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 100):
        other = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not other.exists():
            return other
    return path


# ------------------------------------------------------------------ Prism
PRISM_COMPONENTS = {
    "fabric": [("net.fabricmc.intermediary", "minecraft"), ("net.fabricmc.fabric-loader", "loader")],
    "quilt": [("net.fabricmc.intermediary", "minecraft"), ("org.quiltmc.quilt-loader", "loader")],
    "neoforge": [("net.neoforged", "loader")],
    "forge": [("net.minecraftforge", "loader")],
    "vanilla": [],
}


def install_prism(joiner: "Joiner", pack: dict, slug: str, data_dir: Path) -> dict:
    """An instance in Prism's instances folder (re-running updates it in place)."""
    inst = data_dir / "instances" / f"mcsm-{slug}"
    game = inst / ".minecraft"
    game.mkdir(parents=True, exist_ok=True)
    components = [{"uid": "net.minecraft", "version": pack["minecraft"], "important": True}]
    for uid, which in PRISM_COMPONENTS[pack["loader"]]:
        components.append({"uid": uid, "version": pack["minecraft"] if which == "minecraft" else pack["loader_version"]})
    (inst / "mmc-pack.json").write_text(json.dumps({"components": components, "formatVersion": 1}, indent=2))
    memory = int(pack.get("memory_gb") or 4) * 1024
    cfg = inst / "instance.cfg"
    old = {}
    if cfg.exists():  # keep settings the person changed in Prism
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            k, sep, v = line.partition("=")
            if sep and not line.startswith("["):
                old[k] = v
    old.update({"ConfigVersion": "1.2", "InstanceType": "OneSix", "name": pack["name"], "JoinServerOnLaunch": "true",
                "JoinServerOnLaunchAddress": pack["address"], "OverrideMemory": "true",
                "MaxMemAlloc": str(memory), "MinMemAlloc": old.get("MinMemAlloc", "512")})
    old.setdefault("iconKey", "default")
    cfg.write_text("[General]\n" + "".join(f"{k}={v}\n" for k, v in old.items()), encoding="utf-8")
    fetched, removed = joiner.sync_mods(pack, game)
    joiner.add_server(pack, game)
    return {"launcher": "prism", "where": str(inst), "downloaded": fetched, "removed": removed,
            "message": f"added \"{pack['name']}\" to Prism Launcher; it joins the server when you press Launch"}


def prism_command(instance_id: str, address: str) -> list[list[str]]:
    args = ["--launch", instance_id, "--server", address]
    if os.name == "nt":
        cands = []
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles")):
            if base:
                for sub in ("Programs/PrismLauncher", "PrismLauncher"):
                    exe = Path(base) / sub / "prismlauncher.exe"
                    if exe.exists():
                        cands.append([str(exe), *args])
        return cands
    if sys.platform == "darwin":
        return [["open", "-a", "Prism Launcher", "--args", *args]]
    cands = []
    if shutil.which("prismlauncher"):
        cands.append(["prismlauncher", *args])
    if shutil.which("flatpak"):
        cands.append(["flatpak", "run", "org.prismlauncher.PrismLauncher", *args])
    return cands


def open_prism(slug: str, address: str) -> bool:
    for cmd in prism_command(f"mcsm-{slug}", address):
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue
    return False


def _pack_settings(z: zipfile.ZipFile, pack: dict) -> None:
    """Switch the friend's resource packs and shaders on in a new instance."""
    from .friendextras import enable_packs
    extras = [m for m in pack.get("mods", []) if m.get("extra")]
    if not any(m.get("folder") in ("resourcepacks", "shaderpacks") for m in extras):
        return
    with tempfile.TemporaryDirectory() as tmp:
        enable_packs(Path(tmp), extras)
        for p in Path(tmp).rglob("*"):
            if p.is_file():
                z.write(p, "overrides/" + p.relative_to(tmp).as_posix())


# --------------------------------------------------------------- Modrinth
MRPACK_LOADERS = {"fabric": "fabric-loader", "quilt": "quilt-loader", "neoforge": "neoforge", "forge": "forge"}


def build_mrpack(joiner: "Joiner", pack: dict, out_dir: Path) -> Path:
    """A Modrinth modpack: Modrinth-hosted mods by link, anything else inside the file."""
    deps = {"minecraft": pack["minecraft"]}
    if pack["loader"] in MRPACK_LOADERS:
        deps[MRPACK_LOADERS[pack["loader"]]] = pack["loader_version"]
    files = []
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        from .join import folder_of
        for m, jar in fetch_mods(joiner, pack, Path(tmp)):
            data = jar.read_bytes()
            if m["url"].startswith(MRPACK_HOSTS):
                files.append({"path": f"{folder_of(m)}/{m['filename']}", "downloads": [m["url"]], "fileSize": len(data),
                              "hashes": {"sha1": hashlib.sha1(data).hexdigest(), "sha512": hashlib.sha512(data).hexdigest()},
                              "env": {"client": "required", "server": "required" if m.get("side") != "client" else "unsupported"}})
            else:
                z.writestr(f"overrides/{folder_of(m)}/{m['filename']}", data)
        z.writestr("overrides/servers.dat", servers_dat(pack))
        _pack_settings(z, pack)
        z.writestr("modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "versionId": str(pack.get("updated") or "1"),
            "name": pack["name"], "summary": f"Plays on {pack['address']} (made by mcsm)",
            "files": files, "dependencies": deps}, indent=2))
    path = _unique(out_dir / f"{_safe_name(pack['name'])}.mrpack")
    path.write_bytes(buf.getvalue())
    return path


def install_modrinth(joiner: "Joiner", pack: dict, out_dir: Path, open_it: bool = True) -> dict:
    path = build_mrpack(joiner, pack, out_dir)
    installed = any(d.is_dir() for d in modrinth_dirs())
    opened = False
    if open_it:  # the Modrinth App imports .mrpack files it's given; otherwise just show where it is
        opened = opener.open_path(path) if installed else (opener.reveal(path) and False)
    return {"launcher": "modrinth", "where": str(path), "opened": opened,
            "message": (f"opened {path.name} with the Modrinth App: confirm the import there" if opened else
                        f"saved {path.name}; in the Modrinth App press + (Create an instance) → Import, and pick it")}


# ------------------------------------------------------------- CurseForge
def cf_loader_id(pack: dict) -> list[dict]:
    if pack["loader"] == "vanilla":
        return []
    return [{"id": f"{pack['loader']}-{pack['loader_version']}", "primary": True}]


def build_curseforge(joiner: "Joiner", pack: dict, out_dir: Path) -> Path:
    """A CurseForge modpack zip, with the mods inside (for this person's own import only)."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        from .join import folder_of
        for m, jar in fetch_mods(joiner, pack, Path(tmp)):
            z.write(jar, f"overrides/{folder_of(m)}/{m['filename']}")
        z.writestr("overrides/servers.dat", servers_dat(pack))
        _pack_settings(z, pack)
        z.writestr("manifest.json", json.dumps({
            "minecraft": {"version": pack["minecraft"], "modLoaders": cf_loader_id(pack)},
            "manifestType": "minecraftModpack", "manifestVersion": 1, "name": pack["name"],
            "version": str(pack.get("updated") or "1"), "author": "mcsm", "files": [], "overrides": "overrides"},
            indent=2))
    path = _unique(out_dir / f"{_safe_name(pack['name'])} (CurseForge).zip")
    path.write_bytes(buf.getvalue())
    return path


def install_curseforge(joiner: "Joiner", pack: dict, out_dir: Path, open_it: bool = True) -> dict:
    path = build_curseforge(joiner, pack, out_dir)
    shown = opener.reveal(path) if open_it else False
    return {"launcher": "curseforge", "where": str(path), "opened": shown,
            "message": f"saved {path.name}; in CurseForge: Minecraft → Create Custom Profile → Import, and pick it"}
