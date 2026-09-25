"""First-time setup: the web setup page, its validation, and `mcsm start` on a new machine."""

import json
import threading

import pytest

from mcsm import cli, config as configmod, lock as lockmod, setup as setupmod
from mcsm.config import ConfigError
from mcsm.daemon import Daemon
from mcsm.properties import read_properties

from test_manager import manager
from test_web import Client, login, wait_for


def spec(**kw):
    return setupmod.SetupSpec.from_dict({"accept_eula": True, **kw})


def test_spec_validation():
    s = spec(loader="fabric", mods=["lithium"], memory_gb=6, motd="  My   cool\nserver  ")
    assert s.mods == ["fabric-api", "lithium"]  # Fabric API added for Fabric mods
    assert s.motd == "My cool server"  # one line, so it can't break server.properties
    assert spec(loader="neoforge", mods=["create"]).mods == ["create"]
    assert spec(loader="fabric").mods == []  # no mods, no Fabric API needed
    with pytest.raises(ConfigError, match="EULA"):
        setupmod.SetupSpec.from_dict({"loader": "fabric"})
    with pytest.raises(ConfigError, match="can't run mods"):
        spec(loader="vanilla", mods=["lithium"])
    for bad in ({"loader": "bukkit"}, {"mods": ["../x"]}, {"minecraft": "1.21; rm"},
                {"memory_gb": 0}, {"port": 80}, {"difficulty": "nightmare"}, {"mods": "lithium"}):
        with pytest.raises(ConfigError):
            spec(**bad)


def test_configure_writes_the_server(tmp_path):
    root = tmp_path / "srv"
    cfg = setupmod.configure(root, spec(loader="neoforge", minecraft="1.21.1", mods=["create"],
                                        optional_mods=["jei"], memory_gb=6, motd="Friends", max_players=8,
                                        difficulty="hard", port=25570, network_access=True))
    assert cfg.server.loader == "neoforge" and cfg.server.minecraft == "1.21.1" and cfg.server.memory == "6G"
    assert [(m.id, m.required) for m in cfg.mods] == [("create", True), ("jei", False)]
    assert cfg.web.host == "0.0.0.0"
    assert cfg.updates.strategy == "mods-only"  # a chosen version is kept
    latest = setupmod.configure(tmp_path / "srv2", spec(loader="fabric"))
    assert latest.updates.strategy == "latest-compatible"  # "newest": the forever server
    props = read_properties(cfg.server.dir / "server.properties")
    assert props["motd"] == "Friends" and props["max-players"] == "8" and props["server-port"] == "25570"
    assert props["difficulty"] == "hard"
    assert "eula=true" in (cfg.server.dir / "eula.txt").read_text()


def test_memory_suggestion():
    assert setupmod.suggested_memory_gb(16) == 8
    assert setupmod.suggested_memory_gb(6) == 3
    assert setupmod.suggested_memory_gb(2) == 2
    assert setupmod.suggested_memory_gb(64) == 8
    assert setupmod.suggested_memory_gb(0) == 4


@pytest.fixture
def pending_daemon(make_config, http, modrinth, fake_java, monkeypatch):
    modrinth.project("FAPI", "fabric-api", "Fabric API")
    modrinth.version("FAPI", "0.1", ["1.21.1"])
    modrinth.project("AAA", "goodmod", "Good Mod")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    cfg = make_config([])
    cfg.web.port = 0
    cfg.web.password = "hunter2hunter2"
    setupmod.mark_pending(cfg.root)

    # configure() writes a fresh mcsm.toml; keep the test's fake java and short timeouts.
    real = configmod.render_template

    def render(loader, minecraft):
        text = real(loader, minecraft).replace('default = "java"', f"default = {json.dumps(str(fake_java))}")
        return text.replace("warn_minutes = [10, 5, 1]", "warn_minutes = []") \
                   .replace('startup_timeout = "10m"', 'startup_timeout = "30s"')
    monkeypatch.setattr(configmod, "render_template", render)

    m = manager(cfg, http, ["1.21.1"])
    d = Daemon(m, tick=0.1)
    t = threading.Thread(target=d.run, kwargs={"web": True}, daemon=True)
    t.start()
    wait_for(lambda: d.ui is not None)
    yield d, Client(d.ui.url.rstrip("/")), cfg
    d.stop_requested.set()
    t.join(20)


def test_web_setup_builds_and_starts_the_server(pending_daemon):
    d, c, cfg = pending_daemon
    login(c)
    status = c.get("/api/status")[1]
    assert status["setup_pending"] and status["state"] == "stopped"  # nothing starts before setup

    opts = c.get("/api/setup")[1]
    assert opts["pending"] and opts["versions"] == ["1.21.1"]
    assert [l["name"] for l in opts["loaders"]][:3] == ["fabric", "neoforge", "forge"]
    assert 1 <= opts["memory_gb"] <= 8

    # Search is filtered by the loader picked on the setup page.
    from mcsm.mods.modrinth import API
    d.m.http.json[f"{API}/search"] = {"hits": [{"project_id": "AAA", "slug": "goodmod", "title": "Good Mod"}]}
    assert c.get("/api/mods/search?loader=fabric&q=good")[1]["results"][0]["slug"] == "goodmod"
    assert c.get("/api/mods/search?loader=bukkit&q=good")[0] == 400

    assert c.post("/api/setup", {"loader": "fabric", "minecraft": "1.21.1", "mods": ["goodmod"]})[0] == 400  # no EULA
    status_code, body, _ = c.post("/api/setup", {"loader": "fabric", "minecraft": "1.21.1", "mods": ["goodmod"],
                                                 "memory_gb": 2, "motd": "Test", "accept_eula": True})
    assert status_code == 200, body
    wait_for(lambda: d.state == "running" and not d.setup_pending, timeout=30)
    lk = lockmod.load(cfg.root)
    assert lk.minecraft == "1.21.1" and {m.name for m in lk.mods} == {"Fabric API", "Good Mod"}
    assert read_properties(cfg.server.dir / "server.properties")["motd"] == "Test"
    assert c.get("/api/status")[1]["setup_pending"] is False
    assert c.post("/api/setup", {"accept_eula": True})[0] == 409  # only once


def test_failed_setup_can_be_retried(pending_daemon):
    d, c, cfg = pending_daemon
    login(c)
    assert c.post("/api/setup", {"loader": "fabric", "mods": ["no-such-mod"], "accept_eula": True})[0] == 200
    wait_for(lambda: d.last_job and d.last_job["name"] == "set up server")
    assert not d.last_job["ok"] and "no-such-mod" in d.last_job["message"]
    assert d.setup_pending and d.state == "stopped"

    assert c.post("/api/setup", {"loader": "fabric", "mods": ["goodmod"], "accept_eula": True})[0] == 200
    wait_for(lambda: d.state == "running" and not d.setup_pending, timeout=30)


def test_start_on_a_new_machine_opens_setup(tmp_path, monkeypatch):
    home = tmp_path / "home" / "mcsm"
    monkeypatch.setenv("MCSM_HOME", str(home))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    ran = []
    monkeypatch.setattr(cli, "_run_daemon", lambda d, web: ran.append((d, web)) or 0)
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    assert cli.main(["-C", str(empty), "start", "--no-browser"]) == 0
    assert (home / "mcsm.toml").exists() and setupmod.is_pending(home)
    d, web = ran[0]
    assert web and d.m.config.root == home.resolve()
    if cli.has_display() is False and __import__("sys").platform.startswith("linux"):
        assert d.m.config.web.host == "0.0.0.0"  # headless: reachable from another device
    # Running start again reuses the same (still pending) server rather than making a new one.
    assert cli.main(["-C", str(empty), "start", "--no-browser"]) == 0
    assert len(ran) == 2 and ran[1][0].m.config.root == home.resolve()
