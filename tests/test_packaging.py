"""The standalone-executable paths: binary self-update, the setup wizard, and no-argument start."""

import hashlib
import os

import pytest

from mcsm import cli, selfupdate
from mcsm.http import HashMismatch
from mcsm.manager import Manager
from mcsm.mods import providers_for

from conftest import FakeLoader, FakeMojang


def publish_binary(http, content: bytes, sums_content: bytes | None = None):
    name = selfupdate.asset_name()
    http.files[f"https://dl.test/{name}"] = content
    digest = hashlib.sha256(sums_content if sums_content is not None else content).hexdigest()
    http.files["https://dl.test/SHA256SUMS.txt"] = f"{digest}  {name}\nabc  other-file\n".encode()
    return selfupdate.Release("0.2.0", "v0.2.0", "https://github.test/r", "", {
        name: f"https://dl.test/{name}", "SHA256SUMS.txt": "https://dl.test/SHA256SUMS.txt"})


def test_asset_names_cover_every_build():
    assert selfupdate.asset_name() in {"mcsm-linux-x64", "mcsm-linux-arm64", "mcsm-macos-arm64",
                                       "mcsm-macos-x64", "mcsm-windows-x64.exe", "mcsm-windows-arm64.exe"}


def test_binary_is_replaced_after_checksum(tmp_path, http):
    folder = tmp_path / "app"
    folder.mkdir()
    exe = folder / "mcsm"
    exe.write_bytes(b"old version")
    release = publish_binary(http, b"new version")
    assert selfupdate.install_binary(release, exe, http) == "installed mcsm 0.2.0"
    assert exe.read_bytes() == b"new version"
    if os.name != "nt":
        assert os.access(exe, os.X_OK)
    assert [p.name for p in folder.iterdir()] == ["mcsm"]  # no temp files left behind


def test_tampered_binary_is_rejected(tmp_path, http):
    exe = tmp_path / "mcsm"
    exe.write_bytes(b"old version")
    release = publish_binary(http, b"evil", sums_content=b"new version")
    with pytest.raises(HashMismatch):
        selfupdate.install_binary(release, exe, http)
    assert exe.read_bytes() == b"old version"


def test_release_without_checksums_is_refused(tmp_path, http):
    exe = tmp_path / "mcsm"
    exe.write_bytes(b"old")
    release = publish_binary(http, b"new")
    del release.assets["SHA256SUMS.txt"]
    with pytest.raises(selfupdate.SelfUpdateError, match="SHA256SUMS"):
        selfupdate.install_binary(release, exe, http)


def test_frozen_build_needs_an_asset_for_this_platform(monkeypatch):
    monkeypatch.setattr(selfupdate, "frozen", lambda: True)
    ok, why = selfupdate.install_method(selfupdate.Release("0.2.0", "v0.2.0", "", "", {}))
    assert not ok and selfupdate.asset_name() in why
    monkeypatch.setattr("sys.argv", ["/opt/mcsm", "run", "--web"])
    assert selfupdate.restart_argv()[1:] == ["run", "--web"]  # the executable itself, same arguments


def test_no_arguments_means_start(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "cmd_start", lambda args: called.append(args.command) or 0)
    assert cli.main([]) == 0
    assert called == ["start"]


def test_wizard_builds_a_server(tmp_path, http, modrinth, fake_java, monkeypatch):
    modrinth.project("FAPI", "fabric-api", "Fabric API")
    modrinth.version("FAPI", "0.1", ["1.21.1"])
    modrinth.project("LITH", "lithium", "Lithium")
    modrinth.version("LITH", "1.0", ["1.21.1"])

    def fake_manager(cfg, echo=True):
        cfg.java_default = str(fake_java)
        mojang = FakeMojang(http, ["1.21.1"])
        return Manager(cfg, http=http, mojang=mojang, loader=FakeLoader(http, mojang),
                       providers=providers_for(cfg, http), echo=False)
    monkeypatch.setattr(cli, "Manager", fake_manager)
    monkeypatch.setattr(cli, "HttpClient", lambda: http)

    answers = iter(["", "1.21.1", "6G", "lithium", "yes"])  # loader: default fabric
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    root = tmp_path / "home" / "mcsm"
    assert cli._wizard(root)
    toml = (root / "mcsm.toml").read_text()
    assert 'id = "fabric-api"' in toml and 'id = "lithium"' in toml  # fabric-api added for fabric
    assert 'memory = "6G"' in toml
    assert "eula=true" in (root / "server" / "eula.txt").read_text()
    assert (root / "server" / "mods" / "LITH-1.0.jar").exists()


def test_wizard_stops_without_eula(tmp_path, monkeypatch):
    answers = iter(["fabric", "latest", "4G", "", "no"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert not cli._wizard(tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_start_defaults_to_home_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("MCSM_HOME", str(tmp_path / "home"))
    assert cli.default_home() == (tmp_path / "home").resolve()
