"""Several servers in one mcsm (`mcsm start`): listed in the web UI, and never started by themselves."""

import json
import threading

import pytest

from mcsm import cli, config as configmod, lock as lockmod, setup as setupmod, webauth
from mcsm.hub import Hub

from test_manager import manager, update
from test_web import Client, wait_for


@pytest.fixture
def fake_template(monkeypatch, fake_java):
    """configure() writes a fresh mcsm.toml; point it at the fake java with short timeouts."""
    real = configmod.render_template

    def render(loader, minecraft):
        text = real(loader, minecraft).replace('default = "java"', f"default = {json.dumps(str(fake_java))}")
        return text.replace("warn_minutes = [10, 5, 1]", "warn_minutes = []") \
                   .replace('startup_timeout = "10m"', 'startup_timeout = "30s"')
    monkeypatch.setattr(configmod, "render_template", render)


@pytest.fixture
def hub_env(tmp_path, http, modrinth, fake_template):
    modrinth.project("FAPI", "fabric-api", "Fabric API")
    modrinth.version("FAPI", "0.1", ["1.21.1"])
    modrinth.project("AAA", "goodmod", "Good Mod")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    home = tmp_path / "home"
    # An installed server in servers/alpha, and a never-finished one in the home folder (mcsm 0.1-0.3).
    alpha = home / "servers" / "alpha"
    setupmod.configure(alpha, setupmod.SetupSpec.from_dict({"loader": "fabric", "minecraft": "1.21.1",
                                                            "motd": "Alpha", "accept_eula": True}))
    assert update(manager(configmod.load(alpha), http, ["1.21.1"])).ok
    setupmod.configure(home, setupmod.SetupSpec.from_dict({"loader": "fabric", "accept_eula": True}))
    setupmod.mark_pending(home)

    hub = Hub(home, make_manager=lambda cfg: manager(cfg, http, ["1.21.1"]), http=http, tick=0.1)
    hub.web.port = 0
    t = threading.Thread(target=hub.run, daemon=True)
    t.start()
    wait_for(lambda: hub.ui is not None and hub.ui.httpd is not None)
    yield hub, Client(hub.ui.url.rstrip("/"))
    hub.stop_requested.set()
    t.join(30)


def login(c):
    assert c.post("/api/login", {"password": "PASSWORD"})[0] == 200


def test_servers_are_listed_and_never_start_by_themselves(hub_env):
    hub, c = hub_env
    login(c)
    overview = c.get("/api/hub")[1]
    assert not overview["single"] and overview["auth"]["default"]
    servers = {s["id"]: s for s in overview["servers"]}
    assert servers["alpha"]["name"] == "Alpha" and servers["alpha"]["minecraft"] == "1.21.1"
    assert servers["main"]["setup_pending"]
    import time
    time.sleep(1.0)
    assert all(s["state"] == "stopped" for s in c.get("/api/hub")[1]["servers"])  # nothing auto-starts

    assert c.get("/api/status")[0] == 404  # with several servers, say which one
    assert c.get("/api/servers/alpha/status")[1]["id"] == "alpha"
    assert c.get("/api/servers/nope/status")[0] == 404

    assert c.post("/api/servers/alpha/server/start")[0] == 200
    wait_for(lambda: hub.get("alpha").state == "running", timeout=30)
    assert hub.get("main").state == "stopped"
    assert c.post("/api/servers/alpha/command", {"command": "say hi"})[0] == 200
    lines = c.get("/api/servers/alpha/console")[1]["lines"]
    assert any(line["text"] == "> say hi" for line in lines)
    # Each server's activity feed only shows its own messages.
    events = [e["message"] for e in c.get("/api/servers/alpha/events")[1]["events"]]
    assert any("starting server" in e for e in events)
    assert not any("starting server" in e["message"] for e in c.get("/api/servers/main/events")[1]["events"])

    assert c.post("/api/servers/alpha/server/stop")[0] == 200
    wait_for(lambda: hub.get("alpha").state == "stopped" and not hub.get("alpha").job, timeout=30)

    # A server that was never installed can be taken off the list (installed ones can't).
    assert c.post("/api/hub/remove", {"id": "alpha"})[0] == 400
    assert c.post("/api/hub/remove", {"id": "main"})[0] == 200
    assert not (hub.home / "mcsm.toml").exists() and any((hub.home / ".mcsm" / "trash").iterdir())
    import time as _t
    _t.sleep(0.5)
    hub.scan()
    assert [s["id"] for s in c.get("/api/hub")[1]["servers"]] == ["alpha"]


def test_create_a_server_from_the_web(hub_env):
    hub, c = hub_env
    login(c)
    opts = c.get("/api/hub/setup")[1]
    assert opts["versions"] == ["1.21.1"] and not opts["network_option"]
    from mcsm.mods.modrinth import API
    hub.http.json[f"{API}/search"] = {"hits": [{"project_id": "AAA", "slug": "goodmod", "title": "Good Mod"}]}
    assert c.get("/api/hub/mods/search?loader=fabric&q=good")[1]["results"][0]["slug"] == "goodmod"

    status, body, _ = c.post("/api/hub/create", {"loader": "fabric", "minecraft": "1.21.1", "mods": ["goodmod"],
                                                  "motd": "My World!", "accept_eula": True})
    assert status == 200, body
    sid = body["id"]
    assert sid == "my-world" and (hub.home / "servers" / "my-world" / "mcsm.toml").exists()
    d = hub.get(sid)
    wait_for(lambda: d.last_job and d.last_job["name"] == "set up server", timeout=30)
    assert d.last_job["ok"], d.last_job
    assert "press Start" in d.last_job["message"]
    assert d.state == "stopped" and not d.setup_pending  # installed and test-booted, but not left running
    lk = lockmod.load(d.m.config.root)
    assert lk.minecraft == "1.21.1" and {m.name for m in lk.mods} == {"Fabric API", "Good Mod"}
    # A second server with the same name gets its own folder, and each gets its own port.
    assert c.post("/api/hub/create", {"loader": "vanilla", "motd": "My World", "accept_eula": True})[1]["id"] == "my-world-2"
    ports = {s["id"]: s["port"] for s in c.get("/api/hub")[1]["servers"] if s["id"] != "main"}
    assert len(set(ports.values())) == 3 and ports["alpha"] == "25565"
    # Two servers can't be given the same port...
    status, body, _ = c.post("/api/servers/my-world/settings", {"port": 25565})
    assert status == 400 and "alpha" in body["error"]
    assert c.post("/api/servers/my-world/settings", {"port": 25600})[0] == 200
    assert c.get("/api/servers/my-world/settings")[1]["port"] == 25600
    # ...and one can't start while another running server is on its port.
    from mcsm.properties import write_properties
    write_properties(d.m.server_dir / "server.properties", {"server-port": "25565"})
    assert c.post("/api/servers/alpha/server/start")[0] == 200
    wait_for(lambda: hub.get("alpha").state == "running", timeout=30)
    assert c.post(f"/api/servers/{sid}/server/start")[0] == 200
    wait_for(lambda: d.last_job and d.last_job["name"] == "start", timeout=30)
    assert not d.last_job["ok"] and "already used by Alpha" in d.last_job["message"] and d.state == "stopped"


def test_password_reset_from_this_computer(hub_env):
    hub, c = hub_env
    login(c)
    assert c.post("/api/auth/change", {"mode": "pin", "secret": "2468"})[0] == 200
    other = Client(c.base)
    assert other.post("/api/login", {"password": "PASSWORD"})[0] == 401
    # Not over the network (or through a proxy)...
    assert other.call("POST", "/api/auth/reset-local", {}, headers={"X-Forwarded-For": "203.0.113.5"})[0] == 403
    # ...but at the server's own computer, back to PASSWORD without a sign-in.
    assert other.post("/api/auth/reset-local", {})[0] == 200
    assert other.post("/api/login", {"password": "PASSWORD"})[0] == 200
    assert c.get("/api/hub")[0] == 401  # everyone else was signed out


def test_password_carried_over_by_020_is_replaced(tmp_path):
    # mcsm 0.2.0 turned 0.1's generated password into web-auth.json (without a format number).
    state = tmp_path / ".mcsm"
    state.mkdir()
    import hashlib
    salt = b"0123456789abcdef"
    (state / "web-auth.json").write_text(json.dumps({
        "mode": "password", "salt": salt.hex(), "iterations": 1000, "default": False,
        "hash": hashlib.pbkdf2_hmac("sha256", b"randomOld", salt, 1000).hex()}))
    hub = Hub(tmp_path)
    auth = webauth.AuthStore(hub).get()
    assert auth.default and auth.check("PASSWORD") and not auth.check("randomOld")
    # Passwords chosen since are kept.
    webauth.AuthStore(hub).set("password", "mine!")
    assert webauth.AuthStore(Hub(tmp_path)).get().check("mine!")


def test_start_opens_the_server_list(tmp_path, monkeypatch, fake_template, capsys):
    home = tmp_path / "home" / "mcsm"
    monkeypatch.setenv("MCSM_HOME", str(home))
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    ran = []
    monkeypatch.setattr(Hub, "run", lambda self: ran.append(self) or 0)

    empty = tmp_path / "elsewhere"
    empty.mkdir()
    assert cli.main(["-C", str(empty), "start", "--no-browser"]) == 0
    assert ran and not (home / "mcsm.toml").exists()  # no placeholder server any more
    out = capsys.readouterr().out
    assert "Create your first server" in out and "Password:       PASSWORD" in out

    # A server folder elsewhere is listed too; a home-folder server that never installed goes to setup.
    other = tmp_path / "other-server"
    setupmod.configure(other, setupmod.SetupSpec.from_dict({"loader": "fabric", "accept_eula": True}))
    setupmod.configure(home, setupmod.SetupSpec.from_dict({"loader": "fabric", "accept_eula": True}))
    assert cli.main(["-C", str(other), "start", "--no-browser"]) == 0
    assert set(ran[-1].discover()) == {"main", "other-server"}
    assert setupmod.is_pending(home)
