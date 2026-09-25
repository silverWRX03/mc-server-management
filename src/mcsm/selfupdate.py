"""Updating mcsm itself from its GitHub releases.

A release is a GitHub release tagged ``vX.Y.Z``. Installing one runs
``python -m pip install --upgrade git+https://github.com/<repo>@<tag>`` with the
same Python that is running mcsm, so it works for pip and pipx installs alike.
Source checkouts (editable installs) are left to ``git pull``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib import metadata

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
    return Release(version=tag.lstrip("vV"), tag=tag, url=data.get("html_url", ""),
                   notes=(data.get("body") or "")[:4000])


def check(http: HttpClient, current: str = __version__) -> Release | None:
    """The newest release, if it is newer than ``current``."""
    release = latest_release(http)
    if release and parse_version(release.version) > parse_version(current):
        return release
    return None


def install_method() -> tuple[bool, str]:
    """(can mcsm update itself, why not)."""
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


def install(release: Release, runner=subprocess.run) -> str:
    ok, why = install_method()
    if not ok:
        raise SelfUpdateError(why)
    spec = f"git+https://github.com/{REPO}@{release.tag}"
    log.info("installing mcsm %s", release.version)
    proc = runner([sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", spec],
                  capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        raise SelfUpdateError(f"pip could not install mcsm {release.version}:\n{tail}")
    return f"installed mcsm {release.version}"


def restart_argv() -> list[str]:
    """The command line to re-run this same mcsm command on the new version."""
    return [sys.executable, "-m", "mcsm", *sys.argv[1:]]
