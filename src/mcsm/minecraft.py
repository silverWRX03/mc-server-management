"""Minecraft release metadata from Mojang's version manifest.

Versions are ordered by the manifest rather than parsed, so both the old
``1.21.x`` scheme and the year-based ``26.x`` scheme compare correctly.
"""

from __future__ import annotations

from dataclasses import dataclass

from .http import HttpClient

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


@dataclass
class VersionInfo:
    id: str
    java_major: int
    server_url: str | None
    server_sha1: str | None


class Mojang:
    def __init__(self, http: HttpClient):
        self.http = http

    def _manifest(self) -> dict:
        return self.http.get_json(MANIFEST_URL)

    def releases(self) -> list[str]:
        """All release versions, oldest first."""
        entries = [v for v in self._manifest()["versions"] if v["type"] == "release"]
        entries.sort(key=lambda v: v["releaseTime"])
        return [v["id"] for v in entries]

    def latest_release(self) -> str:
        return self._manifest()["latest"]["release"]

    def release_time(self, version: str) -> float | None:
        """When a version came out (seconds since the epoch)."""
        from datetime import datetime
        entry = next((v for v in self._manifest()["versions"] if v["id"] == version), None)
        try:
            return datetime.fromisoformat(entry["releaseTime"].replace("Z", "+00:00")).timestamp() if entry else None
        except (KeyError, ValueError):
            return None

    def is_release(self, version: str) -> bool:
        return version in self.releases()

    def newer_than(self, current: str | None) -> list[str]:
        """Releases newer than ``current``, newest first."""
        releases = self.releases()
        if current is None or current not in releases:
            return list(reversed(releases))
        return list(reversed(releases[releases.index(current) + 1:]))

    def compare(self, a: str, b: str) -> int:
        releases = self.releases()
        ia, ib = releases.index(a), releases.index(b)
        return (ia > ib) - (ia < ib)

    def info(self, version: str) -> VersionInfo:
        entry = next((v for v in self._manifest()["versions"] if v["id"] == version), None)
        if entry is None:
            raise ValueError(f"unknown Minecraft version {version!r}")
        meta = self.http.get_json(entry["url"])
        server = meta.get("downloads", {}).get("server", {})
        return VersionInfo(
            id=version,
            java_major=meta.get("javaVersion", {}).get("majorVersion", 8),
            server_url=server.get("url"),
            server_sha1=server.get("sha1"),
        )
