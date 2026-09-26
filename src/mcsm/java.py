"""Choosing, downloading and managing the Java runtime the server runs on.

Managed runtimes are Eclipse Temurin builds from the Adoptium API, installed per
major version under ``.mcsm/java/<major>/``.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import shutil
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .http import HttpClient

log = logging.getLogger(__name__)

ADOPTIUM = "https://api.adoptium.net/v3"
META = "mcsm-java.json"


class JavaError(Exception):
    pass


_VERSION = re.compile(r'version "([^"]+)"')


def parse_major(version_output: str) -> int | None:
    m = _VERSION.search(version_output)
    if not m:
        return None
    parts = m.group(1).split(".")
    if parts[0] == "1" and len(parts) > 1:  # "1.8.0_392" style
        return int(parts[1])
    return int(re.match(r"\d+", parts[0]).group(0))


def probe(binary: str) -> int | None:
    if shutil.which(binary) is None:
        return None
    try:
        from .desktop import NO_WINDOW
        out = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=30, **NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_major(out.stderr + out.stdout)


def host_platform() -> tuple[str, str]:
    """(os, architecture) in Adoptium's vocabulary."""
    system = platform.system().lower()
    if system == "darwin":
        os_name = "mac"
    elif system == "windows":
        os_name = "windows"
    elif Path("/etc/alpine-release").exists():
        os_name = "alpine-linux"
    else:
        os_name = "linux"
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64",
            "armv7l": "arm", "ppc64le": "ppc64le", "s390x": "s390x"}.get(machine, machine)
    return os_name, arch


@dataclass
class ManagedJava:
    major: int
    release: str
    semver: str
    binary: Path

    @property
    def dir(self) -> Path:
        return self.binary.parents[1]


class JavaManager:
    def __init__(self, config: Config, http: HttpClient | None = None,
                 probe_fn: Callable[[str], int | None] = probe,
                 platform_fn: Callable[[], tuple[str, str]] = host_platform):
        self.config = config
        self.http = http or HttpClient()
        self.probe = probe_fn
        self.platform = platform_fn

    @property
    def dir(self) -> Path:
        return self.config.state_dir / "java"

    # ------------------------------------------------------------ managed
    def installed(self) -> dict[int, ManagedJava]:
        out = {}
        if not self.dir.is_dir():
            return out
        for meta in self.dir.glob(f"*/{META}"):
            try:
                d = json.loads(meta.read_text())
                binary = meta.parent / d["java"]
            except (ValueError, KeyError):
                continue
            if binary.exists():
                out[d["major"]] = ManagedJava(d["major"], d["release"], d["semver"], binary)
        return dict(sorted(out.items()))

    def latest_release(self, major: int) -> dict:
        os_name, arch = self.platform()
        for image in dict.fromkeys([self.config.java_image, "jre", "jdk"]):
            assets = self.http.get_json(
                f"{ADOPTIUM}/assets/latest/{major}/hotspot",
                params={"architecture": arch, "image_type": image, "os": os_name, "vendor": "eclipse"})
            if assets:
                return assets[0]
        raise JavaError(f"Temurin has no Java {major} build for {os_name}/{arch}; "
                        f"install one yourself and set it under [java.versions]")

    def install(self, major: int) -> ManagedJava:
        release = self.latest_release(major)
        package = release["binary"]["package"]
        self.dir.mkdir(parents=True, exist_ok=True)
        archive = self.dir / ".download" / package["name"]
        log.info("downloading Java %s (%s)", major, release["release_name"])
        self.http.download(package["link"], archive, sha256=package.get("checksum"))

        extract = self.dir / f".extract-{major}"
        shutil.rmtree(extract, ignore_errors=True)
        extract.mkdir()
        try:
            if package["name"].endswith(".zip"):
                with zipfile.ZipFile(archive) as z:
                    z.extractall(extract)
            else:
                with tarfile.open(archive) as t:
                    t.extractall(extract, filter="data")
            binary = _find_java(extract)
            if binary is None:
                raise JavaError(f"no bin/java in {package['name']}")
            binary.chmod(binary.stat().st_mode | 0o111)
            (extract / META).write_text(json.dumps({
                "major": major, "release": release["release_name"],
                "semver": release.get("version", {}).get("semver", ""),
                "java": binary.relative_to(extract).as_posix(),
            }, indent=2))
            target = self.dir / str(major)
            shutil.rmtree(target, ignore_errors=True)
            extract.rename(target)
        finally:
            shutil.rmtree(extract, ignore_errors=True)
            shutil.rmtree(archive.parent, ignore_errors=True)
        return self.installed()[major]

    def update(self) -> list[tuple[int, str, str]]:
        """Move every managed runtime to its newest patch release."""
        changed = []
        for major, current in self.installed().items():
            latest = self.latest_release(major)["release_name"]
            if latest != current.release:
                self.install(major)
                changed.append((major, current.release, latest))
        return changed

    def remove(self, major: int) -> bool:
        target = self.dir / str(major)
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True

    # ---------------------------------------------------------- selection
    def select(self, required: int, install: bool = True) -> str:
        """Pick the Java binary for a Minecraft version that needs Java ``required``.

        Preference: the exact major version (from [java.versions], managed runtimes,
        then [java] default), downloading it when ``auto_install`` is on; otherwise the
        lowest newer version available. ``[java] version`` forces a major version.
        """
        forced = self.config.java_version
        if forced is not None and forced < required:
            raise JavaError(f"[java] version = {forced}, but this Minecraft version needs Java {required}+")
        wanted = forced or required
        managed = self.installed()
        tried = []

        def ok(binary: str, want) -> bool:
            major = self.probe(binary)
            tried.append(f"{binary} ({'not found' if major is None else f'Java {major}'})")
            return major is not None and want(major)

        exact = lambda m: m == wanted  # noqa: E731
        if wanted in self.config.java_versions and ok(self.config.java_versions[wanted], exact):
            return self.config.java_versions[wanted]
        if wanted in managed:
            return str(managed[wanted].binary)
        if ok(self.config.java_default, exact):
            return self.config.java_default
        if self.config.java_auto_install and install:
            return str(self.install(wanted).binary)
        if forced is None:
            newer = lambda m: m >= required  # noqa: E731
            for major, path in sorted(self.config.java_versions.items()):
                if major >= required and ok(path, newer):
                    return path
            for major, java in managed.items():
                if major >= required:
                    return str(java.binary)
            if self.probe(self.config.java_default) and self.probe(self.config.java_default) >= required:
                return self.config.java_default
        raise JavaError(
            f"Minecraft needs Java {wanted}; tried {', '.join(tried) or 'nothing'}. "
            f"Run `mcsm java install {wanted}`, or set it under [java.versions] in mcsm.toml")


def _find_java(root: Path) -> Path | None:
    hits = [p for name in ("java", "java.exe") for p in root.rglob(f"bin/{name}") if p.is_file()]
    return min(hits, key=lambda p: len(p.parts)) if hits else None
