"""CurseForge API v1 - https://docs.curseforge.com/rest-api/ (needs an API key)."""

from __future__ import annotations

import re

from ..config import ModSpec
from ..http import HttpClient, HttpError
from .base import CHANNEL_RANK, ModError, ModFile, ModProvider, Project, Unavailable

API = "https://api.curseforge.com/v1"
WEBSITE = "https://www.curseforge.com/minecraft/mc-mods"
MINECRAFT_GAME_ID = 432
MODS_CLASS_ID = 6
LOADER_TYPES = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
RELEASE_TYPES = {1: "release", 2: "beta", 3: "alpha"}
REQUIRED_DEPENDENCY = 3
_MC_VERSION = re.compile(r"^\d+(\.\d+)+$")


class CurseForgeProvider(ModProvider):
    source = "curseforge"

    def __init__(self, http: HttpClient, api_key: str):
        self.http = http
        self.api_key = api_key

    def _get(self, path: str, params: dict | None = None):
        if not self.api_key:
            raise ModError("CurseForge mods need an API key: set [curseforge] api_key or MCSM_CURSEFORGE_API_KEY")
        return self.http.get_json(f"{API}{path}", params=params, headers={"x-api-key": self.api_key})["data"]

    def project(self, mod_id: str) -> Project:
        try:
            if mod_id.isdigit():
                m = self._get(f"/mods/{mod_id}")
            else:
                hits = self._get("/mods/search", {"gameId": MINECRAFT_GAME_ID, "classId": MODS_CLASS_ID,
                                                  "slug": mod_id})
                if not hits:
                    raise ModError(f"no CurseForge mod with slug {mod_id!r}")
                m = hits[0]
        except HttpError as e:
            if e.status == 404:
                raise ModError(f"no CurseForge mod {mod_id!r}") from e
            raise
        return Project(source=self.source, id=str(m["id"]), slug=m.get("slug", ""), name=m["name"],
                       server_side="unknown")

    def _files(self, project_id: str, loader: str, minecraft: str | None) -> list[dict]:
        out, index = [], 0
        while True:
            params = {"modLoaderType": LOADER_TYPES[loader], "pageSize": 50, "index": index}
            if minecraft:
                params["gameVersion"] = minecraft
            page = self._get(f"/mods/{project_id}/files", params)
            out.extend(page)
            index += 50
            if len(page) < 50 or index >= 1000:
                return out

    @staticmethod
    def _acceptable(f: dict, channel: str) -> bool:
        return CHANNEL_RANK[RELEASE_TYPES.get(f.get("releaseType"), "alpha")] <= CHANNEL_RANK[channel]

    def supported_versions(self, spec: ModSpec, loaders: tuple[str, ...], channel: str) -> set[str]:
        project = self.project(spec.id)
        out: set[str] = set()
        for loader in loaders:
            if loader not in LOADER_TYPES:
                continue
            for f in self._files(project.id, loader, None):
                if self._acceptable(f, channel):
                    out.update(v for v in f.get("gameVersions", []) if _MC_VERSION.match(v))
        return out

    def resolve(self, spec: ModSpec, minecraft: str, loaders: tuple[str, ...], channel: str) -> ModFile:
        project = self.project(spec.id)
        for loader in loaders:
            if loader not in LOADER_TYPES:
                continue
            files = [f for f in self._files(project.id, loader, minecraft)
                     if minecraft in f.get("gameVersions", []) and self._acceptable(f, channel)]
            if not files:
                continue
            f = max(files, key=lambda f: f.get("fileDate", ""))
            # Authors can opt out of third-party downloads; then a person has to fetch
            # the file from the website, and mcsm picks it up from the manual folder.
            manual_url = None if f.get("downloadUrl") else manual_download_url(project, f["id"])
            sha1 = next((h["value"] for h in f.get("hashes", []) if h.get("algo") == 1), None)
            deps = [str(d["modId"]) for d in f.get("dependencies", [])
                    if d.get("relationType") == REQUIRED_DEPENDENCY]
            return ModFile(
                key=project.key, source=self.source, project_id=project.id, name=project.name,
                version_id=str(f["id"]), version_number=f.get("displayName", f["fileName"]),
                filename=f["fileName"], url=f.get("downloadUrl") or "", sha1=sha1,
                dependencies=deps, required=spec.required, dependency_of=spec.dependency_of,
                manual_url=manual_url,
            )
        raise Unavailable(f"{project.name} has no {'/'.join(loaders)} build for {minecraft}")


def manual_download_url(project: Project, file_id: int | str) -> str:
    if project.slug:
        return f"{WEBSITE}/{project.slug}/files/{file_id}"
    return f"https://www.curseforge.com/projects/{project.id}"
