"""A friend's own extras on top of a server's download: shaders, resource packs and more mods.

They're picked on the friend's page (mcfui), kept on the friend's computer per server, and
installed with the server's mods into the right folders (mods/, resourcepacks/, shaderpacks/)
of each launcher. Mods bring their required mods along; shaders bring a shader loader (Iris,
or Oculus on Forge), which needs a mod loader.

When the server moves to a new Minecraft version, each extra is looked up again. Mods without
a build for it are left out (the friend is asked first); packs without one are kept but
switched off, since old packs often still load; the friend can switch them back on.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .config import ModSpec
from .http import HttpClient, HttpError
from .mods.base import ModError, Unavailable
from .mods.modrinth import API, ModrinthProvider

KINDS = {
    # kind: (folder in the game directory, Modrinth project type, label)
    "mod": ("mods", "mod", "mods"),
    "resourcepack": ("resourcepacks", "resourcepack", "resource packs"),
    "shader": ("shaderpacks", "shader", "shaders"),
}
# The shader loader to add with shaders, by mod loader (Modrinth slugs).
SHADER_LOADERS = {"fabric": "iris", "quilt": "iris", "neoforge": "iris", "forge": "oculus"}
MOD_LOADERS = {"fabric": ("fabric",), "quilt": ("quilt", "fabric"), "neoforge": ("neoforge",), "forge": ("forge",)}
FILE_NAME = re.compile(r"[^/\\:*?\"<>|]{1,200}\.(jar|zip)")
ID = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class ExtrasError(Exception):
    pass


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "server"


class Store:
    """The friend's choices for one server: ~/.minecraft/mcsm/extras/<server>.json."""

    def __init__(self, mc_dir: Path, server_name: str):
        self.path = mc_dir / "mcsm" / "extras" / f"{slugify(server_name)}.json"

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"minecraft": None, "items": []}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def add(self, kind: str, project_id: str, slug: str, name: str) -> dict:
        if kind not in KINDS or not ID.fullmatch(project_id):
            raise ExtrasError("that isn't something mcsm can add")
        data = self.load()
        if any(i["id"] == project_id for i in data["items"]):
            raise ExtrasError(f"{name} is already added")
        data["items"].append({"kind": kind, "id": project_id, "slug": slug if ID.fullmatch(slug or "") else project_id,
                              "name": str(name)[:100], "enabled": True, "added": time.time()})
        self.save(data)
        return data

    def remove(self, project_id: str) -> dict:
        data = self.load()
        data["items"] = [i for i in data["items"] if i["id"] != project_id]
        self.save(data)
        return data

    def set_enabled(self, project_id: str, enabled: bool) -> dict:
        data = self.load()
        for i in data["items"]:
            if i["id"] == project_id:
                i["enabled"] = bool(enabled)
        self.save(data)
        return data


# ------------------------------------------------------------------ search
def search(http: HttpClient, kind: str, query: str, pack: dict, offset: int = 0) -> list[dict]:
    """Modrinth projects of this kind for the server's Minecraft (and loader, for mods)."""
    if kind not in KINDS:
        raise ExtrasError("unknown kind")
    if kind == "mod" and pack["loader"] not in MOD_LOADERS:
        raise ExtrasError("this server runs plain Minecraft, so players can't add mods; resource packs work")
    if kind == "shader" and pack["loader"] not in SHADER_LOADERS:
        raise ExtrasError("shaders need a mod loader (Iris runs on Fabric, Quilt and NeoForge; Oculus on Forge), "
                          "and this server runs plain Minecraft")
    facets = [[f"project_type:{KINDS[kind][1]}"], [f"versions:{pack['minecraft']}"]]
    if kind == "mod":
        facets.append([f"categories:{x}" for x in MOD_LOADERS[pack["loader"]]])
        facets.append(["client_side:required", "client_side:optional"])
    data = http.get_json(f"{API}/search", params={"query": query[:100], "facets": json.dumps(facets),
                                                   "index": "relevance" if query else "downloads",
                                                   "limit": 20, "offset": max(0, int(offset))})
    return [{"id": h["project_id"], "slug": h.get("slug", ""), "name": h.get("title", ""),
             "summary": h.get("description", ""), "icon": h.get("icon_url") or "", "downloads": h.get("downloads", 0),
             "kind": kind} for h in data.get("hits", [])]


# ---------------------------------------------------------------- resolving
def _file_entry(project, version: dict, folder: str, needed_by: str | None = None) -> dict:
    files = version.get("files") or []
    f = next((x for x in files if x.get("primary")), files[0] if files else None)
    if not f or not FILE_NAME.fullmatch(str(f.get("filename", ""))):
        raise Unavailable(f"{project.name} has no usable file")
    hashes = f.get("hashes") or {}
    entry = {"name": project.name, "filename": f["filename"], "url": f["url"], "sha1": hashes.get("sha1"),
             "sha512": hashes.get("sha512"), "project": f"modrinth:{project.id}", "side": "client",
             "folder": folder, "extra": True, "version": version.get("version_number", "")}
    if needed_by:
        entry["needed_by"] = needed_by
    return entry


def _pack_version(provider: ModrinthProvider, project_id: str, kind: str, minecraft: str) -> dict | None:
    loaders = ("minecraft",) if kind == "resourcepack" else ("iris", "optifine")
    try:
        versions = provider._versions(project_id, loaders, minecraft)
    except HttpError as e:
        if e.status == 404:
            return None
        raise
    ok = [v for v in versions if minecraft in v.get("game_versions", [])]
    releases = [v for v in ok if v.get("version_type", "release") == "release"] or ok
    return max(releases, key=lambda v: v.get("date_published", "")) if releases else None


def resolve(http: HttpClient, data: dict, pack: dict, included: set[str]) -> tuple[list[dict], list[dict]]:
    """Files for the friend's extras on this server's Minecraft, and what can't be had for it.

    ``included``: projects already in the server's download (not added twice)."""
    provider = ModrinthProvider(http)
    mc, loader = pack["minecraft"], pack["loader"]
    entries: list[dict] = []
    problems: list[dict] = []
    seen = set(included)
    todo: list[tuple[str, str, str | None]] = []   # (project id or slug, kind, needed by)
    for item in data["items"]:
        todo.append((item["id"], item["kind"], None))
    if any(i["kind"] == "shader" for i in data["items"]) and loader in SHADER_LOADERS:
        todo.append((SHADER_LOADERS[loader], "mod", "your shaders"))
    by_id = {i["id"]: i for i in data["items"]}
    while todo:
        pid, kind, needed_by = todo.pop(0)
        item = by_id.get(pid)
        try:
            project = provider.project(pid)
        except (ModError, HttpError) as e:
            problems.append({"id": pid, "name": item["name"] if item else pid, "kind": kind, "reason": str(e)})
            continue
        if f"modrinth:{project.id}" in seen:
            continue
        seen.add(f"modrinth:{project.id}")
        try:
            if kind == "mod":
                if loader not in MOD_LOADERS:
                    raise Unavailable("this server runs plain Minecraft, which can't run mods")
                f = provider.resolve(ModSpec("modrinth", project.id), mc, MOD_LOADERS[loader], "release", side="client")
                version = {"version_number": f.version_number, "files": [{"filename": f.filename, "url": f.url,
                           "primary": True, "hashes": {"sha1": f.sha1, "sha512": f.sha512}}]}
                entries.append(_file_entry(project, version, "mods", needed_by))
                todo += [(dep, "mod", project.name) for dep in f.dependencies if f"modrinth:{dep}" not in seen]
            else:
                version = _pack_version(provider, project.id, kind, mc)
                if version is None:
                    if item and item.get("file"):  # keep the one they have, switched off
                        entries.append({**item["file"], "enabled": False})
                    raise Unavailable(f"no {KINDS[kind][2][:-1]} build of {project.name} for Minecraft {mc}")
                entry = _file_entry(project, version, KINDS[kind][0])
                entry["enabled"] = bool(item.get("enabled", True)) if item else True
                entries.append(entry)
                if item:
                    item["file"] = {k: v for k, v in entry.items() if k != "enabled"}
        except (Unavailable, ModError) as e:
            problems.append({"id": project.id, "name": project.name, "kind": kind, "reason": str(e),
                             "needed_by": needed_by})
    return entries, problems


def changes_for(data: dict, pack: dict, problems: list[dict]) -> list[dict]:
    """What happens to each extra that doesn't fit the server's new Minecraft, if the friend goes on."""
    out = []
    for p in problems:
        if p.get("needed_by") and p["kind"] == "mod":
            p = {**p, "reason": f"{p['reason']} (needed by {p['needed_by']})"}
        action = "removed" if p["kind"] == "mod" else "switched off"
        out.append({**p, "action": action})
    return out


def apply_changes(store: Store, data: dict, pack: dict, problems: list[dict]) -> tuple[dict, list[str]]:
    """The friend chose to go on: leave out mods that don't fit, keep packs but switch them off."""
    notes = []
    bad = {p["id"] for p in problems}
    items = []
    for i in data["items"]:
        if i["id"] in bad and i["kind"] == "mod":
            notes.append(f"{i['name']} was removed: it has no build for Minecraft {pack['minecraft']} yet")
            continue
        if i["id"] in bad and i.get("enabled", True):
            i["enabled"] = False
            notes.append(f"{i['name']} was switched off: it may not work on Minecraft {pack['minecraft']}. You can switch "
                         "it back on, but the game may crash")
        items.append(i)
    data = {**data, "items": items, "minecraft": pack["minecraft"]}
    store.save(data)
    return data, notes


# ------------------------------------------------------------ in the game
def enable_packs(game_dir: Path, entries: list[dict], gone: tuple[str, ...] = ()) -> None:
    """Switch the friend's resource packs and shaders on (or off) in this game directory;
    ``gone``: pack files just removed (so they're forgotten in the settings too)."""
    packs = [e for e in entries if e.get("folder") == "resourcepacks"]
    shaders = [e for e in entries if e.get("folder") == "shaderpacks"]
    if packs or gone:
        opts = game_dir / "options.txt"
        lines = opts.read_text(encoding="utf-8", errors="replace").splitlines() if opts.exists() else []
        current: list[str] = []
        for line in lines:
            if line.startswith("resourcePacks:"):
                try:
                    current = [str(x) for x in json.loads(line.split(":", 1)[1])]
                except ValueError:
                    current = []
        mine = {f"file/{e['filename']}" for e in packs} | {f"file/{g}" for g in gone}
        on = [f"file/{e['filename']}" for e in packs if e.get("enabled", True)]
        wanted = [x for x in current if x not in mine] or ["vanilla"]
        wanted += [x for x in on if x not in wanted]
        new_line = "resourcePacks:" + json.dumps(wanted, separators=(",", ":"))
        lines = [ln for ln in lines if not ln.startswith("resourcePacks:")] + [new_line]
        game_dir.mkdir(parents=True, exist_ok=True)
        opts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if shaders:
        on = next((e for e in shaders if e.get("enabled", True)), None)
        props = game_dir / "config" / "iris.properties"
        found = {}
        if props.exists():
            for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
                k, sep, v = line.partition("=")
                if sep and not line.startswith("#"):
                    found[k.strip()] = v.strip()
        found["enableShaders"] = "true" if on else "false"
        if on:
            found["shaderPack"] = on["filename"]
        props.parent.mkdir(parents=True, exist_ok=True)
        props.write_text("#Set up by mcsm\n" + "".join(f"{k}={v}\n" for k, v in found.items()), encoding="utf-8")
        # Oculus (Forge) reads its own file
        if (game_dir / "mods").is_dir() and any(p.name.lower().startswith("oculus") for p in (game_dir / "mods").glob("*.jar")):
            (game_dir / "config" / "oculus.properties").write_text(props.read_text(encoding="utf-8"), encoding="utf-8")
