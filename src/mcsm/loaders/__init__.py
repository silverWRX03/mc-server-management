from __future__ import annotations

from ..http import HttpClient
from ..minecraft import Mojang
from .base import Loader, LoaderError, Runtime
from .fabric import FabricLoader, QuiltLoader
from .forge import ForgeLoader, NeoForgeLoader
from .vanilla import VanillaLoader

LOADERS: dict[str, type[Loader]] = {
    cls.name: cls for cls in (FabricLoader, QuiltLoader, NeoForgeLoader, ForgeLoader, VanillaLoader)
}


def get_loader(name: str, http: HttpClient, mojang: Mojang) -> Loader:
    return LOADERS[name](http, mojang)


__all__ = ["Loader", "LoaderError", "Runtime", "LOADERS", "get_loader"]
