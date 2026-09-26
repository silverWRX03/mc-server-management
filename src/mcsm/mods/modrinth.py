"""Modrinth API v2 - https://docs.modrinth.com/api/"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from ..config import ModSpec
from ..http import HttpClient, HttpError
from .base import CHANNEL_RANK, ClientOnly, ModError, ModFile, ModProvider, Project, Unavailable

API = "https://api.modrinth.com/v2"
PLUGIN_LOADERS = ("paper", "spigot", "bukkit", "purpur", "folia")


class ModrinthProvider(ModProvider):
    source = "modrinth"

    def __init__(self, http: HttpClient):
        self.http = http

    def project(self, mod_id: str) -> Project:
        try:
            p = self.http.get_json(f"{API}/project/{mod_id}")
        except HttpError as e:
            if e.status == 404:
                raise ModError(f"no Modrinth project named {mod_id!r}") from e
            raise
        return Project(source=self.source, id=p["id"], slug=p["slug"], name=p["title"],
                       server_side=p.get("server_side", "unknown"), client_side=p.get("client_side", "unknown"))

    def projects(self, ids: list[str]) -> dict[str, dict]:
        """Several projects in one request: id -> project data."""
        if not ids:
            return {}
        data = self.http.get_json(f"{API}/projects", params={"ids": json.dumps(sorted(set(ids)))})
        return {p["id"]: p for p in data}

    def _versions(self, project_id: str, loaders: tuple[str, ...], minecraft: str | None = None) -> list[dict]:
        # One request per project; filtering by game version happens locally so that
        # checking many candidate Minecraft versions stays cheap. With ``minecraft``, only
        # that version's builds are fetched: much smaller for mods with long histories.
        params = {"loaders": json.dumps(list(loaders))}
        if minecraft:
            params["game_versions"] = json.dumps([minecraft])
        return self.http.get_json(f"{API}/project/{project_id}/version", params=params)

    def best_channels(self, project_ids: list[str], loaders: tuple[str, ...], minecraft: str,
                      workers: int = 6) -> dict[str, str | None]:
        """For each project, the most stable kind of build it has for these loaders and this
        Minecraft: "release", "beta" or "alpha", or None when it has none. Search results can't
        say (their version and loader lists cover all of a mod's builds together), so this looks
        at each mod's builds, several at a time. A mod that can't be looked up counts as "release"
        rather than being hidden."""
        def one(pid: str) -> str | None:
            try:
                versions = self._versions(pid, loaders, minecraft)
            except HttpError:
                return "release"
            kinds = {v.get("version_type", "release") for v in versions if minecraft in v.get("game_versions", [])}
            return min(kinds, key=lambda k: CHANNEL_RANK.get(k, 9)) if kinds else None
        ids = list(dict.fromkeys(project_ids))
        if not ids:
            return {}
        with ThreadPoolExecutor(max_workers=min(workers, len(ids))) as pool:
            return dict(zip(ids, pool.map(one, ids)))

    @staticmethod
    def _acceptable(version: dict, channel: str) -> bool:
        return CHANNEL_RANK.get(version.get("version_type", "release"), 9) <= CHANNEL_RANK[channel]

    def supported_versions(self, spec: ModSpec, loaders: tuple[str, ...], channel: str) -> set[str]:
        project = self.project(spec.id)
        out: set[str] = set()
        for v in self._versions(project.id, loaders):
            if self._acceptable(v, channel):
                out.update(v.get("game_versions", []))
        return out

    def resolve(self, spec: ModSpec, minecraft: str, loaders: tuple[str, ...], channel: str,
                side: str = "server") -> ModFile:
        project = self.project(spec.id)
        if side == "server" and project.server_side == "unsupported":
            raise ClientOnly(f"{project.name} is client-side only")
        if side == "client" and project.client_side == "unsupported":
            raise Unavailable(f"{project.name} only runs on servers")
        try:
            versions = self._versions(project.id, loaders)  # all of them: reused for other Minecraft versions
        except HttpError as e:
            if e.status == 404:
                raise
            # Mods with long histories (Fabric API has thousands of builds) can time out;
            # ask for just this Minecraft version's builds instead.
            versions = self._versions(project.id, loaders, minecraft)
        candidates = [v for v in versions
                      if minecraft in v.get("game_versions", []) and self._acceptable(v, channel)]
        if not candidates:
            raise Unavailable(f"{project.name} has no {'/'.join(loaders)} build for {minecraft}")
        # Prefer the loader listed first (e.g. a native Quilt build over a Fabric one),
        # then the newest publish date.
        def rank(v):
            loader_rank = min((loaders.index(l) for l in v.get("loaders", []) if l in loaders), default=99)
            return (-loader_rank, v.get("date_published", ""))
        version = max(candidates, key=rank)
        files = version.get("files", [])
        if not files:
            raise Unavailable(f"{project.name} {version['version_number']} has no files")
        f = next((f for f in files if f.get("primary")), files[0])
        deps = [d["project_id"] for d in version.get("dependencies", [])
                if d.get("dependency_type") == "required" and d.get("project_id")]
        return ModFile(
            key=project.key, source=self.source, project_id=project.id, name=project.name,
            version_id=version["id"], version_number=version["version_number"],
            filename=f["filename"], url=f["url"],
            sha1=f.get("hashes", {}).get("sha1"), sha512=f.get("hashes", {}).get("sha512"),
            dependencies=deps, required=spec.required, dependency_of=spec.dependency_of,
        )

    def identify(self, sha1_hashes: list[str]) -> dict[str, dict]:
        """Map jar sha1 -> Modrinth version (used to import an existing mods folder)."""
        if not sha1_hashes:
            return {}
        return self.http.post_json(f"{API}/version_files", {"hashes": sha1_hashes, "algorithm": "sha1"})

    def search(self, query: str, loaders: tuple[str, ...], limit: int = 20, index: str = "relevance",
               side: str = "server", minecraft: str | None = None) -> list[dict]:
        """Mods for the server (or, with ``side="client"``, for players' computers) matching
        ``query``, most relevant first (or most downloaded). With ``minecraft``, only mods
        that have a build for that version."""
        facets = [[f"categories:{l}" for l in loaders], [f"{side}_side:required", f"{side}_side:optional"],
                  ["project_type:mod"]]
        if any(l in PLUGIN_LOADERS for l in loaders):
            facets = [[f"categories:{l}" for l in loaders]]  # server plugins: their loaders say it all
        if minecraft:
            facets.append([f"versions:{minecraft}"])
        data = self.http.get_json(f"{API}/search", params={
            "query": query, "limit": limit, "index": index, "facets": json.dumps([f for f in facets if f])})
        return [{
            "id": h["project_id"], "slug": h.get("slug", ""), "name": h.get("title", ""),
            "description": h.get("description", ""), "icon": h.get("icon_url") or "",
            "downloads": h.get("downloads", 0), "server_side": h.get("server_side", "unknown"),
            "client_side": h.get("client_side", "unknown"),
            "latest_version": h.get("latest_version", ""),
        } for h in data.get("hits", [])]


def keep_buildable(channels: dict[str, str | None], hits: list[dict], early: bool, key: str = "id") -> dict:
    """Search results that really have a build for the chosen Minecraft and loader, each
    marked with its most stable ``channel``; ones with only alpha/beta builds only when
    ``early``. Also says how many were left out, and why."""
    out, hidden, early_hidden = [], 0, 0
    for hit in hits:
        channel = channels.get(hit[key], "release")
        if channel is None:
            hidden += 1
        elif channel != "release" and not early:
            early_hidden += 1
        else:
            out.append({**hit, "channel": channel})
    return {"results": out, "hidden": hidden, "early_hidden": early_hidden}
