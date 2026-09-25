from __future__ import annotations

from pathlib import Path

from .base import Loader, Runtime, run_installer

FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
QUILT_MAVEN = "https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer"


def _first_stable(entries: list[dict], key=lambda e: e) -> str | None:
    for e in entries:
        inner = key(e)
        if inner.get("stable", True):
            return inner["version"]
    return None


class FabricLoader(Loader):
    name = "fabric"
    mod_loaders = ("fabric",)
    launcher = "fabric-server-launch.jar"

    def latest_version(self, minecraft: str) -> str | None:
        def fetch():
            entries = self.http.get_json(f"{FABRIC_META}/versions/loader/{minecraft}")
            return _first_stable(entries, key=lambda e: e["loader"]) if entries else None
        return self._safe_latest(fetch)

    def install(self, minecraft: str, version: str, dest: Path, java: str) -> Runtime:
        installer = _first_stable(self.http.get_json(f"{FABRIC_META}/versions/installer"))
        url = f"{FABRIC_META}/versions/loader/{minecraft}/{version}/{installer}/server/jar"
        self.http.download(url, dest / self.launcher)
        # The launcher fetches the vanilla server jar itself on first start.
        return Runtime(files=[self.launcher], launch=["-jar", self.launcher, "nogui"])


class QuiltLoader(Loader):
    name = "quilt"
    # Quilt runs most Fabric mods.
    mod_loaders = ("quilt", "fabric")
    launcher = "quilt-server-launch.jar"

    def latest_version(self, minecraft: str) -> str | None:
        def fetch():
            entries = self.http.get_json(f"{QUILT_META}/versions/loader/{minecraft}")
            for e in entries:
                v = e["loader"]["version"]
                if "beta" not in v and "pre" not in v:
                    return v
            return None
        return self._safe_latest(fetch)

    def install(self, minecraft: str, version: str, dest: Path, java: str) -> Runtime:
        installer_version = self.http.get_json(f"{QUILT_META}/versions/installer")[0]["version"]
        jar = dest / "quilt-installer.jar"
        self.http.download(f"{QUILT_MAVEN}/{installer_version}/quilt-installer-{installer_version}.jar", jar)
        run_installer(java, jar, ["install", "server", minecraft, version, "--download-server",
                                  f"--install-dir={dest}"], cwd=dest)
        return Runtime(files=[self.launcher, "server.jar", "libraries"],
                       launch=["-jar", self.launcher, "nogui"])
