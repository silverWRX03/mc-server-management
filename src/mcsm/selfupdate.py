"""Updating mcsm itself from its GitHub releases.

A release is a GitHub release tagged ``vX.Y.Z``. How it is installed depends on
how mcsm was installed:

* the standalone download (a PyInstaller executable): the matching executable is
  downloaded from the release, checked against the release's ``SHA256SUMS.txt``,
  and swapped in place of the running one;
* pip / pipx: ``python -m pip install --upgrade git+https://github.com/<repo>@<tag>``
  with the same Python that runs mcsm;
* a source checkout (editable install): left to ``git pull``.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path

from . import __version__
from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

REPO = "silverWRX03/mc-server-management"
DIST = "mc-server-management"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"


class SelfUpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    url: str
    notes: str
    assets: dict[str, str] = field(default_factory=dict)   # file name -> download URL

    def to_dict(self) -> dict:
        return asdict(self)


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v.lstrip("vV").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:3]) or (0,)


def latest_release(http: HttpClient) -> Release | None:
    try:
        data = http.get_json(LATEST, headers={"Accept": "application/vnd.github+json"})
    except HttpError as e:
        if e.status == 404:  # no releases published yet
            return None
        raise
    tag = data.get("tag_name", "")
    if not tag or data.get("draft") or data.get("prerelease"):
        return None
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])
              if a.get("name") and a.get("browser_download_url")}
    return Release(version=tag.lstrip("vV"), tag=tag, url=data.get("html_url", ""),
                   notes=(data.get("body") or "")[:4000], assets=assets)


def check(http: HttpClient, current: str = __version__) -> Release | None:
    """The newest release, if it is newer than ``current``."""
    release = latest_release(http)
    if release and parse_version(release.version) > parse_version(current):
        return release
    return None


def frozen() -> bool:
    """True when running as the standalone executable."""
    return bool(getattr(sys, "frozen", False))


def asset_name() -> str:
    """The release file for this computer, e.g. ``mcsm-windows-x64.exe``."""
    system = {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return f"mcsm-{system}-{arch}{'.exe' if system == 'windows' else ''}"


def install_method(release: Release | None = None) -> tuple[bool, str]:
    """(can mcsm update itself, why not)."""
    if frozen():
        if release is not None and asset_name() not in release.assets:
            return False, f"this release has no download for your system ({asset_name()}); get it from the release page"
        return True, ""
    try:
        dist = metadata.distribution(DIST)
    except metadata.PackageNotFoundError:
        return False, "mcsm is running from a source checkout; update it with `git pull`"
    direct = dist.read_text("direct_url.json")
    if direct:
        try:
            if json.loads(direct).get("dir_info", {}).get("editable"):
                return False, "mcsm is installed in editable (development) mode; update it with `git pull`"
        except ValueError:
            pass
    return True, ""


def install(release: Release, runner=subprocess.run, http: HttpClient | None = None) -> str:
    ok, why = install_method(release)
    if not ok:
        raise SelfUpdateError(why)
    if frozen():
        return install_binary(release, Path(sys.executable), http or HttpClient())
    spec = f"git+https://github.com/{REPO}@{release.tag}"
    log.info("installing mcsm %s", release.version)
    proc = runner([sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", spec],
                  capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        raise SelfUpdateError(f"pip could not install mcsm {release.version}:\n{tail}")
    return f"installed mcsm {release.version}"


def _expected_sha256(release: Release, name: str, http: HttpClient, workdir: Path) -> str:
    sums_url = release.assets.get("SHA256SUMS.txt")
    if not sums_url:
        raise SelfUpdateError("the release has no SHA256SUMS.txt, so the download can't be verified")
    sums = http.download(sums_url, workdir / "SHA256SUMS.txt").read_text()
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    raise SelfUpdateError(f"SHA256SUMS.txt has no entry for {name}")


def install_binary(release: Release, exe: Path, http: HttpClient) -> str:
    """Download this platform's executable, verify it, and put it in place of ``exe``."""
    name = asset_name()
    if name not in release.assets:
        raise SelfUpdateError(f"this release has no download for your system ({name})")
    log.info("downloading mcsm %s (%s)", release.version, name)
    try:
        with tempfile.TemporaryDirectory(dir=exe.parent, prefix=".mcsm-update-") as tmp:
            workdir = Path(tmp)
            sha256 = _expected_sha256(release, name, http, workdir)
            new = http.download(release.assets[name], workdir / name, sha256=sha256)
            new.chmod(0o755)
            if os.name == "nt":
                # A running .exe can't be overwritten, but it can be renamed out of the way.
                old = old_binary(exe)
                old.unlink(missing_ok=True)
                exe.rename(old)
                try:
                    os.replace(new, exe)
                except OSError:
                    old.rename(exe)
                    raise
            else:
                os.replace(new, exe)
    except PermissionError as e:
        raise SelfUpdateError(f"no permission to replace {exe}; move mcsm somewhere you can write to, "
                              f"or download the new version from {release.url}") from e
    return f"installed mcsm {release.version}"


def old_binary(exe: Path) -> Path:
    return exe.with_name(exe.stem + ".old" + exe.suffix)


def cleanup_after_update() -> None:
    """Delete the previous executable that a Windows self-update left behind."""
    if frozen():
        try:
            old_binary(Path(sys.executable)).unlink(missing_ok=True)
        except OSError:
            pass


def restart_argv() -> list[str]:
    """The command line to re-run this same mcsm command on the new version."""
    if frozen():
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, "-m", "mcsm", *sys.argv[1:]]
