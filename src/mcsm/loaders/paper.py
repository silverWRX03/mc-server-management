from __future__ import annotations

from pathlib import Path

from ..http import HttpError
from .base import Loader, LoaderError, Runtime

FILL = "https://fill.papermc.io/v3/projects/paper"
PAPER_V2 = "https://api.papermc.io/v2/projects/paper"   # older API, used if Fill can't be reached
JAR = "paper.jar"


class PaperLoader(Loader):
    """Paper: a fast vanilla-compatible server that runs plugins (not mods), from ``plugins/``.

    Builds come from PaperMC's Fill API; only stable builds are used. The loader "version"
    is the build number.
    """
    name = "paper"
    #: Modrinth loaders for plugins Paper runs
    mod_loaders = ("paper", "spigot", "bukkit")
    mods_folder = "plugins"

    def _builds(self, minecraft: str) -> list[dict]:
        """Stable builds, newest first, as {id, name, url, sha256}."""
        try:
            builds = self.http.get_json(f"{FILL}/versions/{minecraft}/builds")
            out = []
            for b in builds if isinstance(builds, list) else []:
                d = (b.get("downloads") or {}).get("server:default") or {}
                if str(b.get("channel", "")).upper() == "STABLE" and d.get("url"):
                    out.append({"id": int(b["id"]), "name": d.get("name") or f"paper-{minecraft}-{b['id']}.jar",
                                "url": d["url"], "sha256": (d.get("checksums") or {}).get("sha256")})
            return sorted(out, key=lambda b: b["id"], reverse=True)
        except HttpError as e:
            if e.status is not None and 400 <= e.status < 500:
                raise
        data = self.http.get_json(f"{PAPER_V2}/versions/{minecraft}/builds")
        out = []
        for b in data.get("builds", []):
            app = (b.get("downloads") or {}).get("application") or {}
            if b.get("channel", "default") == "default" and app.get("name"):
                out.append({"id": int(b["build"]), "name": app["name"], "sha256": app.get("sha256"),
                            "url": f"{PAPER_V2}/versions/{minecraft}/builds/{b['build']}/downloads/{app['name']}"})
        return sorted(out, key=lambda b: b["id"], reverse=True)

    def latest_version(self, minecraft: str) -> str | None:
        def fetch():
            builds = self._builds(minecraft)
            return str(builds[0]["id"]) if builds else None
        return self._safe_latest(fetch)

    def install(self, minecraft: str, version: str, dest: Path, java: str) -> Runtime:
        build = next((b for b in self._builds(minecraft) if str(b["id"]) == str(version)), None)
        if build is None:
            raise LoaderError(f"Paper build {version} for Minecraft {minecraft} isn't available")
        self.http.download(build["url"], dest / JAR, sha256=build["sha256"])
        # Paper fetches and patches the vanilla server into cache/ and libraries/ on first start.
        return Runtime(files=[JAR], launch=["-jar", JAR, "--nogui"])
