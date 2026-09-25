from __future__ import annotations

from pathlib import Path

from .base import Loader, LoaderError, Runtime


class VanillaLoader(Loader):
    name = "vanilla"
    mod_loaders = ()

    def latest_version(self, minecraft: str) -> str | None:
        return minecraft if self.mojang.info(minecraft).server_url else None

    def install(self, minecraft: str, version: str, dest: Path, java: str) -> Runtime:
        info = self.mojang.info(minecraft)
        if not info.server_url:
            raise LoaderError(f"Minecraft {minecraft} has no server download")
        self.http.download(info.server_url, dest / "server.jar", sha1=info.server_sha1)
        return Runtime(files=["server.jar"], launch=["-jar", "server.jar", "nogui"])
