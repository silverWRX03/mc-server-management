"""Searching Modrinth (and CurseForge, with an API key) for the mod browser window.

Results and project pages are normalised to one shape so the web UI can show either
source the same way. Descriptions come back as Markdown (Modrinth) or HTML
(CurseForge); the page sanitises them before showing them.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

from .http import HttpClient, HttpError
from .mods import curseforge as cf
from .mods.base import ModError
from .mods.base import CHANNEL_RANK
from .mods.modrinth import API as MODRINTH, ModrinthProvider, keep_buildable

SORTS = ("relevance", "downloads", "follows", "newest", "updated")
CF_SORT = {"relevance": None, "downloads": 6, "follows": 2, "newest": 11, "updated": 3}
CF_MODPACKS_CLASS_ID = 4471
PAGE = 20


class BrowseError(Exception):
    pass


class Browser:
    def __init__(self, http: HttpClient, curseforge_key: str = ""):
        self.http = http
        self.cf_key = curseforge_key
        self._categories: dict[tuple, tuple[float, list]] = {}

    @property
    def sources(self) -> list[str]:
        return ["modrinth", "curseforge"] if self.cf_key else ["modrinth"]

    # ------------------------------------------------------------ search
    def search(self, source: str = "modrinth", kind: str = "mod", query: str = "", loader: str | None = None,
               version: str | None = None, category: str | None = None, sort: str = "relevance",
               offset: int = 0, early: bool = False, side: str = "server") -> dict:
        """One page of results. For mods with a loader and a Minecraft version, only ones that
        really have a build for both (``early``: including ones with only alpha/beta builds)."""
        if kind not in ("mod", "modpack"):
            raise BrowseError("unknown kind")
        if sort not in SORTS:
            raise BrowseError("unknown sort order")
        offset = max(0, min(int(offset), 10_000))
        plugins = loader == "paper"
        if side not in ("server", "client"):
            raise BrowseError("unknown side")
        if source == "curseforge":
            if side == "client":
                raise BrowseError("mods for players come from Modrinth; switch the source to Modrinth")
            if plugins:
                raise BrowseError("Paper plugins come from Modrinth; switch the source to Modrinth")
            if kind == "modpack":
                raise BrowseError("CurseForge modpacks aren't supported yet; search Modrinth")
            return self._cf_search(query, loader, version, category, sort, offset, early)
        if source != "modrinth":
            raise BrowseError("unknown source")
        facets = [[f"project_type:{kind}"], [f"{side}_side:required", f"{side}_side:optional"]]
        if plugins and kind == "mod":
            facets = [["categories:paper", "categories:spigot", "categories:bukkit"]]
        elif loader and kind == "mod":
            facets.append([f"categories:{loader}"] + (["categories:fabric"] if loader == "quilt" else []))
        if loader and kind == "modpack":
            facets.append([f"categories:{loader}"])
        if version:
            facets.append([f"versions:{version}"])
        if category:
            facets.append([f"categories:{category}"])
        data = self.http.get_json(f"{MODRINTH}/search", params={
            "query": query, "facets": json.dumps(facets), "index": sort, "offset": offset, "limit": PAGE})
        hits = [{
            "source": "modrinth", "id": h["project_id"], "slug": h.get("slug", ""), "name": h.get("title", ""),
            "summary": h.get("description", ""), "icon": h.get("icon_url") or "", "author": h.get("author", ""),
            "downloads": h.get("downloads", 0), "follows": h.get("follows", 0),
            "updated": h.get("date_modified", ""), "created": h.get("date_created", ""),
            "categories": h.get("display_categories") or h.get("categories", []),
            "versions": h.get("versions", [])[-6:], "kind": kind,
            "url": f"https://modrinth.com/{'plugin' if plugins else kind}/{h.get('slug') or h['project_id']}",
        } for h in data.get("hits", [])]
        page = {"total": data.get("total_hits", len(hits)), "offset": offset, "page": PAGE}
        if kind == "mod" and loader and version and hits:
            from .loaders import LOADERS
            loaders = LOADERS[loader].mod_loaders if loader in LOADERS else (loader,)
            channels = ModrinthProvider(self.http).best_channels([h["id"] for h in hits], loaders, version)
            return {**keep_buildable(channels, hits, early), **page}
        return {"results": hits, "hidden": 0, "early_hidden": 0, **page}

    def _cf(self, path: str, params: dict | None = None):
        if not self.cf_key:
            raise BrowseError("searching CurseForge needs a CurseForge API key (Settings)")
        return self.http.get_json(f"{cf.API}{path}", params=params, headers={"x-api-key": self.cf_key})["data"]

    def _cf_channels(self, ids: list[str], loader: str, version: str) -> dict[str, str | None]:
        """Like ModrinthProvider.best_channels, from each CurseForge project's files."""
        def one(mod_id: str) -> str | None:
            try:
                files = self._cf(f"/mods/{mod_id}/files", {"gameVersion": version, "modLoaderType": cf.LOADER_TYPES[loader],
                                                          "pageSize": 50})
            except HttpError:
                return "release"
            kinds = {cf.RELEASE_TYPES.get(f.get("releaseType"), "alpha") for f in files
                     if version in f.get("gameVersions", [])}
            return min(kinds, key=lambda k: CHANNEL_RANK[k]) if kinds else None
        with ThreadPoolExecutor(max_workers=min(6, len(ids))) as pool:
            return dict(zip(ids, pool.map(one, ids)))

    def _cf_search(self, query, loader, version, category, sort, offset, early=False) -> dict:
        params = {"gameId": cf.MINECRAFT_GAME_ID, "classId": cf.MODS_CLASS_ID, "searchFilter": query,
                  "index": offset, "pageSize": PAGE, "sortOrder": "desc"}
        if CF_SORT[sort]:
            params["sortField"] = CF_SORT[sort]
        if loader in cf.LOADER_TYPES:
            params["modLoaderType"] = cf.LOADER_TYPES[loader]
        if version:
            params["gameVersion"] = version
        if category and category.isdigit():
            params["categoryId"] = category
        hits = [{
            "source": "curseforge", "id": str(m["id"]), "slug": m.get("slug", ""), "name": m.get("name", ""),
            "summary": m.get("summary", ""), "icon": (m.get("logo") or {}).get("thumbnailUrl", ""),
            "author": ", ".join(a.get("name", "") for a in m.get("authors", [])[:2]),
            "downloads": int(m.get("downloadCount", 0)), "follows": 0,
            "updated": m.get("dateModified", ""), "created": m.get("dateReleased", ""),
            "categories": [c.get("name", "") for c in m.get("categories", [])][:4], "versions": [],
            "kind": "mod", "url": (m.get("links") or {}).get("websiteUrl") or f"{cf.WEBSITE}/{m.get('slug', '')}",
        } for m in self._cf("/mods/search", params)]
        page = {"total": offset + len(hits) + (PAGE if len(hits) == PAGE else 0), "offset": offset, "page": PAGE}
        if loader in cf.LOADER_TYPES and version and hits:
            return {**keep_buildable(self._cf_channels([h["id"] for h in hits], loader, version), hits, early), **page}
        return {"results": hits, "hidden": 0, "early_hidden": 0, **page}

    # ----------------------------------------------------------- details
    def project(self, source: str, project_id: str) -> dict:
        if source == "curseforge":
            m = self._cf(f"/mods/{project_id}")
            try:
                body = self._cf(f"/mods/{project_id}/description")
            except (HttpError, BrowseError):
                body = m.get("summary", "")
            return {
                "source": "curseforge", "id": str(m["id"]), "slug": m.get("slug", ""), "name": m.get("name", ""),
                "summary": m.get("summary", ""), "icon": (m.get("logo") or {}).get("thumbnailUrl", ""),
                "body": body, "body_format": "html", "downloads": int(m.get("downloadCount", 0)), "follows": 0,
                "updated": m.get("dateModified", ""), "license": "",
                "categories": [c.get("name", "") for c in m.get("categories", [])],
                "gallery": [{"url": s.get("url", ""), "title": s.get("title", "")} for s in m.get("screenshots", [])][:8],
                "links": {k: v for k, v in (m.get("links") or {}).items() if isinstance(v, str) and v.startswith("https://")},
                "url": (m.get("links") or {}).get("websiteUrl") or f"{cf.WEBSITE}/{m.get('slug', '')}",
                "loaders": [], "game_versions": [], "kind": "mod", "versions": [],
            }
        if source != "modrinth":
            raise BrowseError("unknown source")
        try:
            p = self.http.get_json(f"{MODRINTH}/project/{project_id}")
        except HttpError as e:
            if e.status == 404:
                raise ModError("that project doesn't exist any more") from e
            raise
        kind = p.get("project_type", "mod")
        out = {
            "source": "modrinth", "id": p["id"], "slug": p.get("slug", ""), "name": p.get("title", ""),
            "summary": p.get("description", ""), "icon": p.get("icon_url") or "", "body": p.get("body", ""),
            "body_format": "markdown", "downloads": p.get("downloads", 0), "follows": p.get("followers", 0),
            "updated": p.get("updated", ""), "license": (p.get("license") or {}).get("name", ""),
            "categories": p.get("categories", []),
            "gallery": [{"url": g.get("url", ""), "title": g.get("title") or ""} for g in p.get("gallery", [])][:8],
            "links": {k: p[k] for k in ("source_url", "issues_url", "wiki_url", "discord_url")
                      if isinstance(p.get(k), str) and p[k].startswith("https://")},
            "url": f"https://modrinth.com/{kind}/{p.get('slug') or p['id']}",
            "loaders": p.get("loaders", []), "game_versions": p.get("game_versions", [])[-12:], "kind": kind,
            "client_side": p.get("client_side"), "server_side": p.get("server_side"), "versions": [],
        }
        if kind == "modpack":
            versions = self.http.get_json(f"{MODRINTH}/project/{p['id']}/version")
            out["versions"] = [{"id": v["id"], "name": v.get("version_number", ""), "minecraft": v.get("game_versions", []),
                                "loaders": v.get("loaders", []), "date": v.get("date_published", "")}
                               for v in versions[:25]]
        return out

    def categories(self, source: str = "modrinth", kind: str = "mod") -> list[dict]:
        key = (source, kind)
        cached = self._categories.get(key)
        if cached and time.monotonic() - cached[0] < 86400:
            return cached[1]
        if source == "curseforge":
            data = self._cf("/categories", {"gameId": cf.MINECRAFT_GAME_ID, "classId": cf.MODS_CLASS_ID})
            out = sorted(({"id": str(c["id"]), "name": c["name"]} for c in data), key=lambda c: c["name"])
        else:
            data = self.http.get_json(f"{MODRINTH}/tag/category")
            out = sorted(({"id": c["name"], "name": c["name"].replace("-", " ").capitalize()} for c in data
                          if c.get("project_type") == kind and c.get("header") == "categories"),
                         key=lambda c: c["name"])
        self._categories[key] = (time.monotonic(), out)
        return out
