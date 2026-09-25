from __future__ import annotations

import hashlib
import json
import stat
import sys
import textwrap
import urllib.parse
from pathlib import Path

import pytest

from mcsm import config as configmod
from mcsm.http import HashMismatch, HttpError
from mcsm.loaders.base import Loader, Runtime
from mcsm.minecraft import MANIFEST_URL, Mojang
from mcsm.mods.modrinth import API as MODRINTH

FAKE_SERVER = textwrap.dedent("""\
    import sys, pathlib
    mods = pathlib.Path("mods")
    if mods.is_dir() and any(p.name.startswith("crash") for p in mods.iterdir()):
        print("[Server thread/ERROR]: mod failed to load", flush=True)
        sys.exit(1)
    print("[12:00:00] [Server thread/INFO]: Done (0.1s)! For help, type \\"help\\"", flush=True)
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "list":
            print("[12:00:01] [Server thread/INFO]: There are 0 of a max of 20 players online:", flush=True)
        elif cmd == "stop":
            print("[12:00:02] [Server thread/INFO]: Stopping the server", flush=True)
            sys.exit(0)
""")


class FakeHttp:
    """Serves canned JSON and file bodies keyed by URL."""

    def __init__(self):
        self.json: dict[str, object] = {}
        self.files: dict[str, bytes] = {}
        self.posts: dict[str, object] = {}
        self.downloads: list[str] = []

    def get_json(self, url, params=None, headers=None):
        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        for key in (full, url):
            if key in self.json:
                value = self.json[key]
                if isinstance(value, Exception):
                    raise value
                return json.loads(json.dumps(value))
        raise HttpError(full, 404, "HTTP 404")

    def post_json(self, url, body, headers=None):
        handler = self.posts.get(url)
        if handler is None:
            return {}
        return handler(body) if callable(handler) else handler

    def download(self, url, dest: Path, sha1=None, sha512=None, headers=None, sha256=None):
        if url not in self.files:
            raise HttpError(url, 404, "HTTP 404")
        data = self.files[url]
        if sha1 and hashlib.sha1(data).hexdigest() != sha1:
            raise HashMismatch(url)
        if sha256 and hashlib.sha256(data).hexdigest() != sha256:
            raise HashMismatch(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        self.downloads.append(url)
        return dest


class FakeMojang(Mojang):
    def __init__(self, http: FakeHttp, releases: list[str]):
        super().__init__(http)
        self.set_releases(releases)

    def set_releases(self, releases: list[str]) -> None:
        versions = []
        for i, v in enumerate(releases):
            url = f"https://meta.test/{v}.json"
            versions.append({"id": v, "type": "release", "url": url,
                             "releaseTime": f"2025-01-{i + 1:02d}T00:00:00+00:00"})
            self.http.json[url] = {"javaVersion": {"majorVersion": 21},
                                   "downloads": {"server": {"url": f"https://files.test/{v}/server.jar"}}}
        versions.append({"id": "99w01a", "type": "snapshot", "url": "x", "releaseTime": "2026-01-01T00:00:00+00:00"})
        self.http.json[MANIFEST_URL] = {"latest": {"release": releases[-1]}, "versions": list(reversed(versions))}


class FakeLoader(Loader):
    name = "fabric"
    mod_loaders = ("fabric",)

    def __init__(self, http, mojang, supported: set[str] | None = None):
        super().__init__(http, mojang)
        self.supported = supported
        self.installs: list[str] = []

    def latest_version(self, minecraft):
        if self.supported is not None and minecraft not in self.supported:
            return None
        return f"loader-{minecraft}"

    def install(self, minecraft, version, dest, java):
        self.installs.append(minecraft)
        (dest / "fake_server.py").write_text(FAKE_SERVER)
        (dest / f"runtime-{minecraft}.txt").write_text(version)
        return Runtime(files=["fake_server.py", f"runtime-{minecraft}.txt"], launch=["fake_server.py"])


class ModrinthFixture:
    """Build Modrinth API responses for fake projects."""

    def __init__(self, http: FakeHttp):
        self.http = http
        self.versions: dict[str, list[dict]] = {}

    def project(self, pid: str, slug: str | None = None, title: str | None = None, server_side="required"):
        slug = slug or pid.lower()
        body = {"id": pid, "slug": slug, "title": title or pid, "server_side": server_side}
        self.http.json[f"{MODRINTH}/project/{pid}"] = body
        self.http.json[f"{MODRINTH}/project/{slug}"] = body
        self.versions[pid] = []
        self._publish(pid)

    def version(self, pid: str, number: str, game_versions: list[str], deps: list[str] = (),
                version_type="release", filename: str | None = None, content: bytes | None = None,
                loaders=("fabric",)):
        vid = f"{pid}-{number}"
        filename = filename or f"{pid}-{number}.jar"
        content = content if content is not None else f"{pid} {number}".encode()
        url = f"https://cdn.test/{vid}/{filename}"
        self.http.files[url] = content
        n = len(self.versions[pid])
        self.versions[pid].append({
            "id": vid, "version_number": number, "version_type": version_type,
            "game_versions": list(game_versions), "loaders": list(loaders),
            "date_published": f"2025-02-{n + 1:02d}T00:00:00Z",
            "files": [{"url": url, "filename": filename, "primary": True,
                       "hashes": {"sha1": hashlib.sha1(content).hexdigest()}}],
            "dependencies": [{"project_id": d, "dependency_type": "required"} for d in deps],
        })
        self._publish(pid)
        return content

    def _publish(self, pid):
        url = f"{MODRINTH}/project/{pid}/version?" + urllib.parse.urlencode({"loaders": json.dumps(["fabric"])})
        self.http.json[url] = list(reversed(self.versions[pid]))


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def modrinth(http):
    return ModrinthFixture(http)


@pytest.fixture
def fake_java(tmp_path):
    path = tmp_path / "bin" / "java"
    path.parent.mkdir()
    path.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        if [ "$1" = "-version" ]; then echo 'openjdk version "21.0.4" 2024-07-16' >&2; exit 0; fi
        while [ $# -gt 0 ]; do case "$1" in -X*) shift;; *) break;; esac; done
        exec {sys.executable} "$@"
        """))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def make_config(tmp_path, fake_java):
    def make(mods=(), minecraft="1.21.1", **updates):
        root = tmp_path / "root"
        root.mkdir(exist_ok=True)
        text = configmod.render_template("fabric", minecraft)
        text = text.replace('default = "java"', f'default = "{fake_java}"')
        text = text.replace("warn_minutes = [10, 5, 1]", "warn_minutes = []")
        text = text.replace('startup_timeout = "10m"', 'startup_timeout = "30s"')
        for key, value in updates.items():
            text = text.replace(f"\n{key} = ", f"\n{key} = {json.dumps(value)}  # ", 1)
        (root / "mcsm.toml").write_text(text)
        for spec in mods:
            configmod.append_mod(root / "mcsm.toml", spec)
        server = root / "server"
        server.mkdir(exist_ok=True)
        (server / "eula.txt").write_text("eula=true\n")
        return configmod.load(root)
    return make
