"""Java runtime management, manual (blocked) downloads, and `mcsm create`."""

import hashlib
import io
import tarfile
import urllib.parse

from mcsm import cli, lock as lockmod
from mcsm.config import ModSpec
from mcsm.java import ADOPTIUM, JavaManager
from mcsm.manager import Manager, ManualDownloadRequired
from mcsm.mods import providers_for
from mcsm.mods.curseforge import API as CURSEFORGE
from mcsm.properties import read_properties

from conftest import FakeLoader, FakeMojang
from test_manager import manager, update

FAKE_JAVA_BIN = b"#!/bin/sh\necho 'openjdk version \"21.0.5\" 2024-10-15' >&2\n"


def publish_temurin(http, major, release):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{release}-jre/bin/java")
        info.size, info.mode = len(FAKE_JAVA_BIN), 0o755
        tar.addfile(info, io.BytesIO(FAKE_JAVA_BIN))
    data = buf.getvalue()
    name = f"OpenJDK{major}U-jre_x64_linux_{release}.tar.gz"
    link = f"https://github.test/temurin/{name}"
    http.files[link] = data
    params = {"architecture": "x64", "image_type": "jre", "os": "linux", "vendor": "eclipse"}
    http.json[f"{ADOPTIUM}/assets/latest/{major}/hotspot?{urllib.parse.urlencode(params)}"] = [{
        "release_name": release, "version": {"semver": release.removeprefix("jdk-")},
        "binary": {"package": {"name": name, "link": link, "checksum": hashlib.sha256(data).hexdigest()}},
    }]


def test_java_install_update_remove(make_config, http):
    cfg = make_config()
    cfg.java_default = "/nonexistent/java"
    jm = JavaManager(cfg, http, platform_fn=lambda: ("linux", "x64"))

    publish_temurin(http, 21, "jdk-21.0.4+7")
    # Needed but missing -> downloaded automatically.
    binary = jm.select(21)
    assert binary.endswith("bin/java")
    assert jm.installed()[21].release == "jdk-21.0.4+7"
    assert jm.probe(binary) == 21  # the extracted binary really runs

    publish_temurin(http, 21, "jdk-21.0.5+11")
    assert jm.update() == [(21, "jdk-21.0.4+7", "jdk-21.0.5+11")]
    assert jm.update() == []
    assert jm.remove(21) and jm.installed() == {}


def test_manual_download_for_blocked_curseforge_mod(make_config, http, modrinth):
    content = b"blocked mod jar"
    http.json[f"{CURSEFORGE}/mods/123"] = {"data": {"id": 123, "slug": "blocked-mod", "name": "Blocked Mod"}}
    http.json[f"{CURSEFORGE}/mods/123/files"] = {"data": [{
        "id": 5550001, "displayName": "Blocked Mod 1.0", "fileName": "blocked-1.0.jar", "releaseType": 1,
        "downloadUrl": None, "gameVersions": ["1.21.1", "Fabric"], "fileDate": "2025-01-01T00:00:00Z",
        "hashes": [{"value": hashlib.sha1(content).hexdigest(), "algo": 1}], "dependencies": [],
    }]}
    cfg = make_config([ModSpec("curseforge", "123")])
    cfg.curseforge_api_key = "test-key"
    m = manager(cfg, http, ["1.21.1"])

    decision, _ = m.check()
    [mod] = m.missing_manual(decision.plan)
    assert mod.manual_url == "https://www.curseforge.com/minecraft/mc-mods/blocked-mod/files/5550001"

    result = update(m)
    assert not result.ok
    assert mod.manual_url in result.message
    assert not m.lock.installed
    assert cfg.manual_dir.is_dir()  # created so the user knows where to put the file

    # A wrong file doesn't count.
    (cfg.manual_dir / "blocked-1.0.jar").write_bytes(b"something else")
    assert m.missing_manual(decision.plan)

    (cfg.manual_dir / "blocked-1.0.jar").write_bytes(content)
    result = update(m)
    assert result.ok, result.message
    assert (cfg.server.dir / "mods" / "blocked-1.0.jar").read_bytes() == content


def test_manual_download_error_lists_links():
    from pathlib import Path

    from mcsm.mods.base import ModFile
    mod = ModFile(key="curseforge:1", source="curseforge", project_id="1", name="X", version_id="9",
                  version_number="1", filename="x.jar", url="", manual_url="https://example.test/x")
    text = str(ManualDownloadRequired([mod], Path("/srv/mc/manual-downloads")))
    assert "https://example.test/x" in text and "/srv/mc/manual-downloads" in text


def test_create_builds_a_whole_server(tmp_path, http, modrinth, fake_java, monkeypatch):
    modrinth.project("AAA", "goodmod", "Good Mod")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    FakeMojang(http, ["1.21.1"])

    def fake_manager(cfg, echo=True):
        cfg.java_default = str(fake_java)
        mojang = FakeMojang(http, ["1.21.1"])
        return Manager(cfg, http=http, mojang=mojang, loader=FakeLoader(http, mojang),
                       providers=providers_for(cfg, http), echo=False)

    monkeypatch.setattr(cli, "Manager", fake_manager)
    monkeypatch.setattr(cli, "HttpClient", lambda: http)

    root = tmp_path / "newserver"
    code = cli.main(["create", str(root), "--minecraft", "1.21.1", "--mod", "goodmod", "--memory", "6G",
                     "--port", "25570", "--motd", "hello", "--rcon", "--accept-eula", "-q"])
    assert code == 0
    props = read_properties(root / "server" / "server.properties")
    assert props["server-port"] == "25570" and props["motd"] == "hello"
    assert props["enable-rcon"] == "true" and len(props["rcon.password"]) > 16
    assert 'memory = "6G"' in (root / "mcsm.toml").read_text()
    assert 'id = "goodmod"' in (root / "mcsm.toml").read_text()
    lk = lockmod.load(root)
    assert lk.minecraft == "1.21.1" and lk.installed
    assert (root / "server" / "mods" / "AAA-1.0.jar").exists()
