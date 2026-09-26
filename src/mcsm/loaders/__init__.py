from __future__ import annotations

from ..http import HttpClient
from ..minecraft import Mojang
from ..mods.modrinth import PLUGIN_LOADERS  # Modrinth loaders for server plugins
from .base import Loader, LoaderError, Runtime
from .fabric import FabricLoader, QuiltLoader
from .forge import ForgeLoader, NeoForgeLoader
from .paper import PaperLoader
from .vanilla import VanillaLoader

LOADERS: dict[str, type[Loader]] = {
    cls.name: cls for cls in (FabricLoader, QuiltLoader, NeoForgeLoader, ForgeLoader, PaperLoader, VanillaLoader)
}


def mods_folder(loader: str) -> str:
    return LOADERS[loader].mods_folder if loader in LOADERS else "mods"


def get_loader(name: str, http: HttpClient, mojang: Mojang) -> Loader:
    return LOADERS[name](http, mojang)


__all__ = ["Loader", "LoaderError", "Runtime", "LOADERS", "PLUGIN_LOADERS", "get_loader", "mods_folder"]
