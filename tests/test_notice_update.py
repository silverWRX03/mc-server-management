"""First-run notice, mcsm self-update, and license listing."""

import subprocess
import threading

import pytest

from mcsm import cli, licenses, notice, selfupdate
from mcsm.config import ModSpec
from mcsm.daemon import Daemon

from test_manager import manager, update
from test_web import Client, login, wait_for


@pytest.fixture
def fresh_user(tmp_path, monkeypatch):
    """A user who has never accepted the notice."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "fresh-xdg"))


# ------------------------------------------------------------------ notice
def test_notice_is_short_and_plain():
    assert 1 <= len(notice.POINTS) <= 10
    text = " ".join(notice.POINTS).lower()
    for must in ("internet", "does not collect", "ai", "no warranty", "backup"):
        assert must in text


def test_acceptance_storage(tmp_path, fresh_user):
    root = tmp_path / "root"
    assert not notice.accepted(root)
    notice.accept(root, by="web")
    assert notice.accepted(root) and not notice.accepted(None)  # web acceptance is per server
    notice.accept(None, by="cli")
    assert notice.accepted(None) and notice.accepted(tmp_path / "other")  # cli acceptance is per user


def test_new_notice_version_needs_new_acceptance(tmp_path, monkeypatch):
    assert notice.accepted(None)  # accepted by the autouse fixture
    monkeypatch.setattr(notice, "NOTICE_VERSION", notice.NOTICE_VERSION + 1)
    assert not notice.accepted(None)


def test_cli_refuses_until_accepted(tmp_path, fresh_user, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    root = tmp_path / "srv"
    assert cli.main(["-C", str(root), "init"]) == 2
    err = capsys.readouterr().err
    assert "does not collect usage data" in err and "mcsm notice --accept" in err
    assert not (root / "mcsm.toml").exists()  # nothing happened

    assert cli.main(["licenses"]) == 0  # always allowed
    assert cli.main(["-C", str(root), "--accept-notice", "init"]) == 0
    assert (root / "mcsm.toml").exists()
    assert cli.main(["-C", str(root), "status"]) == 0  # remembered


def test_cli_interactive_prompt(tmp_path, fresh_user, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert cli.main(["-C", str(tmp_path), "init"]) == 2
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    assert cli.main(["-C", str(tmp_path), "init"]) == 0
    assert notice.accepted(None)


def test_licenses_listed():
    text = licenses.as_text()
    for name in ("Apache-2.0", "PSF-2.0", "LGPL-2.1", "GPL-2.0 with Classpath Exception", "Minecraft EULA"):
        assert name in text


# ------------------------------------------------------------- self-update
def release(http, tag, **extra):
    http.json[selfupdate.LATEST] = {"tag_name": tag, "html_url": f"https://github.test/{tag}", "body": "notes", **extra}


def test_version_compare():
    assert selfupdate.parse_version("v0.10.0") > selfupdate.parse_version("0.9.9")
    assert selfupdate.parse_version("1.0.0-rc1") == (1, 0, 0)


def test_check(http):
    assert selfupdate.check(http, "0.1.0") is None  # no releases yet (404)
    release(http, "v0.1.0")
    assert selfupdate.check(http, "0.1.0") is None
    release(http, "v0.2.0")
    r = selfupdate.check(http, "0.1.0")
    assert r.version == "0.2.0" and r.tag == "v0.2.0" and r.notes == "notes"
    release(http, "v0.3.0", prerelease=True)
    assert selfupdate.check(http, "0.1.0") is None


def test_install_uses_pip_with_the_release_tag(monkeypatch):
    monkeypatch.setattr(selfupdate, "install_method", lambda: (True, ""))
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    r = selfupdate.Release("0.2.0", "v0.2.0", "", "")
    assert selfupdate.install(r, runner) == "installed mcsm 0.2.0"
    assert calls[0][1:5] == ["-m", "pip", "install", "--upgrade"]
    assert calls[0][-1] == "git+https://github.com/silverWRX03/mc-server-management@v0.2.0"

    failing = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "ERROR: no such tag")  # noqa: E731
    with pytest.raises(selfupdate.SelfUpdateError, match="no such tag"):
        selfupdate.install(r, failing)


def test_source_checkouts_are_not_self_updated(monkeypatch):
    monkeypatch.setattr(selfupdate, "install_method", lambda: (False, "running from a source checkout"))
    with pytest.raises(selfupdate.SelfUpdateError, match="source checkout"):
        selfupdate.install(selfupdate.Release("0.2.0", "v0.2.0", "", ""))


# --------------------------------------------------------------------- web
@pytest.fixture
def web_daemon(make_config, http, modrinth, fresh_user):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "AAA")])
    cfg.web.port = 0
    cfg.web.password = "hunter2hunter2"
    m = manager(cfg, http, ["1.21.1"])
    assert update(m).ok
    d = Daemon(m, tick=0.1)
    t = threading.Thread(target=d.run, kwargs={"web": True}, daemon=True)
    t.start()
    wait_for(lambda: d.ui is not None)
    yield d, Client(d.ui.url.rstrip("/")), cfg
    d.stop_requested.set()
    t.join(20)


def test_web_requires_notice_before_anything(web_daemon):
    d, c, cfg = web_daemon
    login(c)
    status = c.get("/api/status")[1]
    assert status["notice_accepted"] is False and status["state"] == "stopped"
    assert c.get("/api/mods")[0] == 428
    assert c.post("/api/server/start")[0] == 428
    n = c.get("/api/notice")[1]
    assert not n["accepted"] and len(n["points"]) <= 10
    assert c.get("/api/licenses")[0] == 200

    assert c.post("/api/notice/accept", {"version": n["version"] - 1})[0] == 409
    assert c.post("/api/notice/accept", {"version": n["version"]})[0] == 200
    wait_for(lambda: d.state == "running")  # the server only starts after acceptance
    assert c.get("/api/mods")[0] == 200


def test_web_self_update(web_daemon, monkeypatch):
    d, c, cfg = web_daemon
    notice.accept(cfg.root, by="web")
    wait_for(lambda: d.state == "running")
    login(c)
    assert c.post("/api/self-update/apply", {"version": "9.9.9"})[0] == 404  # nothing available

    d.self_update = {"version": "9.9.9", "tag": "v9.9.9", "url": "u", "notes": "", "current": "0.1.0",
                     "can_install": True, "reason": ""}
    assert c.get("/api/status")[1]["self_update"]["version"] == "9.9.9"
    installed = []
    monkeypatch.setattr(selfupdate, "install", lambda r: installed.append(r.tag) or f"installed mcsm {r.version}")
    assert c.post("/api/self-update/apply", {"version": "9.9.8"})[0] == 409
    assert c.post("/api/self-update/apply", {"version": "9.9.9"})[0] == 200
    wait_for(lambda: d.restart_requested and d.stop_requested.is_set())
    assert installed == ["v9.9.9"]


def test_web_self_update_refused_for_source_installs(web_daemon):
    d, c, cfg = web_daemon
    notice.accept(cfg.root, by="web")
    login(c)
    d.self_update = {"version": "9.9.9", "tag": "v9.9.9", "url": "u", "notes": "", "current": "0.1.0",
                     "can_install": False, "reason": "running from a source checkout"}
    status, body, _ = c.post("/api/self-update/apply", {"version": "9.9.9"})
    assert status == 400 and "source checkout" in body["error"]
