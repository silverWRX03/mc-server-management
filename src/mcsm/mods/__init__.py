from __future__ import annotations

from ..config import Config
from ..http import HttpClient
from .base import ModError, ModFile, ModProvider, Project, Unavailable
from .curseforge import CurseForgeProvider
from .modrinth import ModrinthProvider


def providers_for(config: Config, http: HttpClient) -> dict[str, ModProvider]:
    return {
        "modrinth": ModrinthProvider(http),
        "curseforge": CurseForgeProvider(http, config.curseforge_api_key),
    }


__all__ = ["ModError", "ModFile", "ModProvider", "Project", "Unavailable", "providers_for",
           "ModrinthProvider", "CurseForgeProvider"]
