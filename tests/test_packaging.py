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
    left = sorted(p.name for p in folder.iterdir())
    # No temp files left behind (Windows keeps the old copy until the next start).
    assert left == (["mcsm", "mcsm.old"] if os.name == "nt" else ["mcsm"])


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

    # loader (default fabric), version, memory, mods, network access, EULA
    answers = iter(["", "1.21.1", "6G", "lithium", "yes", "yes"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    root = tmp_path / "home" / "mcsm"
    assert cli._wizard(root)
    toml = (root / "mcsm.toml").read_text()
    assert 'id = "fabric-api"' in toml and 'id = "lithium"' in toml  # fabric-api added for fabric
    assert 'memory = "6G"' in toml
    assert "eula=true" in (root / "server" / "eula.txt").read_text()
    assert (root / "server" / "mods" / "LITH-1.0.jar").exists()
    assert 'host = "0.0.0.0"' in toml  # reachable from other devices, as asked


def test_wizard_stops_without_eula(tmp_path, monkeypatch):
    answers = iter(["fabric", "latest", "4G", "", "no", "no"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert not cli._wizard(tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_start_defaults_to_home_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("MCSM_HOME", str(tmp_path / "home"))
    assert cli.default_home() == (tmp_path / "home").resolve()


def test_headless_linux_has_no_browser(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not cli.has_display()
    monkeypatch.setenv("DISPLAY", ":0")
    assert cli.has_display()


# ------------------------------------------------------------------ service
def test_service_unit_user_and_system(tmp_path, monkeypatch):
    from mcsm import service
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(service, "mcsm_command", lambda: ["/opt/my apps/mcsm"])
    root = tmp_path / "My Server"
    user = service.plan(root, system=False)
    assert user.name == "mcsm-my-server.service"
    assert user.path == tmp_path / "cfg" / "systemd" / "user" / "mcsm-my-server.service"
    assert 'ExecStart="/opt/my apps/mcsm" run --web' in user.text
    assert f"WorkingDirectory={root}" in user.text and "WantedBy=default.target" in user.text
    system = service.plan(root, system=True)
    assert str(system.path).replace("\\", "/") == "/etc/systemd/system/mcsm-my-server.service"
    assert "WantedBy=multi-user.target" in system.text and system.systemctl == ["systemctl"]


def test_service_install_and_uninstall(tmp_path, monkeypatch):
    import subprocess

    from mcsm import notice, service
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(service, "supported", lambda: True)
    calls = []

    def runner(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[0] == "loginctl" else 0, "", "not allowed")
    root = tmp_path / "srv"
    root.mkdir()
    messages = service.install(root, runner, system=False)
    unit = tmp_path / "cfg" / "systemd" / "user" / "mcsm-srv.service"
    assert unit.exists()
    assert ["systemctl", "--user", "enable", "--now", "mcsm-srv.service"] in calls
    assert any("sudo loginctl enable-linger" in m for m in messages)  # linger failed -> tell the user
    assert notice.accepted(root)  # the service can't answer the prompt, so it's recorded for this server

    assert service.uninstall(root, runner, system=False) == "removed mcsm-srv.service"
    assert not unit.exists()
    with pytest.raises(service.ServiceError):
        service.uninstall(root, runner, system=False)


def test_cmd_goes_through_the_real_parser(tmp_path, capsys):
    """`mcsm cmd` once crashed: its argument clashed with the subcommand's name."""
    assert cli.main(["-C", str(tmp_path), "--accept-notice", "init"]) == 0
    assert cli.main(["-C", str(tmp_path), "cmd", "say", "hello"]) == 1  # RCON is off: a clean error
    assert "RCON is disabled" in capsys.readouterr().out


def test_panel_service_for_a_computer_without_a_screen(tmp_path, monkeypatch, capsys):
    """`mcsm service install --panel`: the whole control panel at boot, managed from another PC."""
    import subprocess

    from mcsm import service
    home = tmp_path / "mcsm"
    monkeypatch.setenv("MCSM_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("MCSM_LAN_IP", "192.168.1.50")
    monkeypatch.setattr(service, "supported", lambda: True)
    monkeypatch.setattr(service, "mcsm_command", lambda: ["/home/me/.local/bin/mcsm"])
    calls = []
    fake = lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "", "")  # noqa: E731
    real_install = service.install
    monkeypatch.setattr(service, "install", lambda root, **kw: real_install(root, runner=fake, system=False, **kw))
    p = service.plan(home, system=False, panel=True)
    assert p.name == "mcsm.service"
    assert "ExecStart=/home/me/.local/bin/mcsm start --no-browser --web-host 0.0.0.0" in p.text
    assert f"Environment=MCSM_HOME={home}" in p.text

    assert cli.main(["service", "install", "--panel"]) == 0
    out = capsys.readouterr().out
    assert "http://192.168.1.50:8765/" in out and "first sign-in password: Mcsm-" in out
    assert ["systemctl", "--user", "enable", "--now", "mcsm.service"] in calls
    import json
    assert json.loads((home / ".mcsm" / "hub.json").read_text())["web"]["host"] == "0.0.0.0"
