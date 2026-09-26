"""Modrinth API v2 - https://docs.modrinth.com/api/"""

from __future__ import annotations

import json

from ..config import ModSpec
from ..http import HttpClient, HttpError
from .base import CHANNEL_RANK, ClientOnly, ModError, ModFile, ModProvider, Project, Unavailable

API = "https://api.modrinth.com/v2"


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
                       server_side=p.get("server_side", "unknown"))

    def _versions(self, project_id: str, loaders: tuple[str, ...]) -> list[dict]:
        # One request per project; filtering by game version happens locally so that
        # checking many candidate Minecraft versions stays cheap.
        return self.http.get_json(f"{API}/project/{project_id}/version",
                                  params={"loaders": json.dumps(list(loaders))})

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

    def resolve(self, spec: ModSpec, minecraft: str, loaders: tuple[str, ...], channel: str) -> ModFile:
        project = self.project(spec.id)
        if project.server_side == "unsupported":
            raise ClientOnly(f"{project.name} is client-side only")
        candidates = [v for v in self._versions(project.id, loaders)
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

    def search(self, query: str, loaders: tuple[str, ...], limit: int = 20, index: str = "relevance") -> list[dict]:
        """Server-compatible mods matching ``query``, most relevant first (or most downloaded)."""
        facets = [[f"categories:{l}" for l in loaders], ["server_side:required", "server_side:optional"],
                  ["project_type:mod"]]
        data = self.http.get_json(f"{API}/search", params={
            "query": query, "limit": limit, "index": index, "facets": json.dumps([f for f in facets if f])})
        return [{
            "id": h["project_id"], "slug": h.get("slug", ""), "name": h.get("title", ""),
            "description": h.get("description", ""), "icon": h.get("icon_url") or "",
            "downloads": h.get("downloads", 0), "server_side": h.get("server_side", "unknown"),
            "latest_version": h.get("latest_version", ""),
        } for h in data.get("hits", [])]
