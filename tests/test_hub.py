"""Several servers in one mcsm (`mcsm start`): listed in the web UI, and never started by themselves."""

import json

from mcsm import cli, lock as lockmod, setup as setupmod, webauth
from mcsm.hub import Hub

from test_web import Client, wait_for


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



def test_delete_servers(hub_env):
    hub, c = hub_env
    login(c)
    alpha = hub.get("alpha").m.config.root
    world = hub.get("alpha").m.server_dir / "world"
    world.mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_text("level")

    # A running server can't be deleted.
    assert c.post("/api/servers/alpha/server/start")[0] == 200
    wait_for(lambda: hub.get("alpha").state == "running", timeout=30)
    assert c.post("/api/hub/delete", {"id": "alpha", "delete_files": True})[0] == 400
    assert c.post("/api/servers/alpha/server/stop")[0] == 200
    wait_for(lambda: hub.get("alpha").state == "stopped" and not hub.get("alpha").job, timeout=30)

    # "Keep the files": off the list (and it stays off), but the world is untouched.
    status, body, _ = c.post("/api/hub/delete", {"id": "alpha", "delete_files": False})
    assert status == 200 and "still in" in body["message"]
    assert (world / "level.dat").read_text() == "level"
    hub.scan()
    assert "alpha" not in {s["id"] for s in c.get("/api/hub")[1]["servers"]}

    # "Delete everything" in the home folder removes only that server's files, not mcsm's own.
    (hub.home / "notes.txt").write_text("mine")
    status, body, _ = c.post("/api/hub/delete", {"id": "main", "delete_files": True})
    assert status == 200, body
    assert not (hub.home / "mcsm.toml").exists() and not (hub.home / "server").exists()
    assert (hub.home / "notes.txt").exists() and (hub.home / ".mcsm" / "web-auth.json").exists()
    assert alpha.exists()  # the kept one is still there

    # A server in mcsm's servers folder is erased completely.
    sid = c.post("/api/hub/create", {"loader": "vanilla", "motd": "Temp", "accept_eula": True})[1]["id"]
    d = hub.get(sid)
    wait_for(lambda: d.last_job and d.last_job["name"] == "set up server", timeout=30)
    root = d.m.config.root
    assert root.exists()
    assert c.post("/api/hub/delete", {"id": sid, "delete_files": True})[0] == 200
    assert not root.exists()
    assert c.get("/api/hub")[1]["servers"] == []


def test_create_a_server_from_the_web(hub_env):
    hub, c = hub_env
    login(c)
    opts = c.get("/api/hub/setup")[1]
    assert opts["versions"] == ["1.21.1"] and not opts["network_option"]
    from mcsm.mods.modrinth import API
    hub.http.json[f"{API}/search"] = {"hits": [{"project_id": "AAA", "slug": "goodmod", "title": "Good Mod"}]}
    assert c.get("/api/hub/mods/search?loader=fabric&q=good")[1]["results"][0]["slug"] == "goodmod"
    # With nothing typed, the setup page lists the 20 most downloaded mods for the loader.
    seen, orig = [], hub.http.get_json
    hub.http.get_json = lambda url, params=None, headers=None: seen.append(params) or orig(url, params, headers)
    assert c.get("/api/hub/mods/search?loader=fabric&top=1")[1]["results"]
    assert seen[-1]["index"] == "downloads" and seen[-1]["limit"] == 20 and seen[-1]["query"] == ""
    assert c.get("/api/hub/mods/search?loader=vanilla&top=1")[1]["results"] == []
    assert c.get("/api/hub/mods/search?loader=fabric")[1]["results"] == []
    hub.http.get_json = orig

    status, body, _ = c.post("/api/hub/create", {"loader": "fabric", "minecraft": "1.21.1", "mods": ["goodmod"],
                                                  "motd": "My World!", "accept_eula": True,
                                                  "properties": {"pvp": False, "view-distance": "12"}})
    assert status == 200, body
    sid = body["id"]
    assert sid == "my-world" and (hub.home / "servers" / "my-world" / "mcsm.toml").exists()
    d = hub.get(sid)
    wait_for(lambda: d.last_job and d.last_job["name"] == "set up server", timeout=30)
    assert d.last_job["ok"], d.last_job
    assert "press Start" in d.last_job["message"]
    assert d.state == "stopped" and not d.setup_pending  # installed and test-booted, but not left running
    from mcsm.properties import read_properties
    assert read_properties(d.m.server_dir / "server.properties")["pvp"] == "false"  # advanced setting
    lk = lockmod.load(d.m.config.root)
    assert lk.minecraft == "1.21.1" and {m.name for m in lk.mods} == {"Fabric API", "Good Mod"}
    # A second server with the same name gets its own folder, and each gets its own port.
    assert c.post("/api/hub/create", {"loader": "vanilla", "motd": "My World", "accept_eula": True})[1]["id"] == "my-world-2"
    ports = {s["id"]: s["port"] for s in c.get("/api/hub")[1]["servers"] if s["id"] != "main"}
    assert len(set(ports.values())) == 3 and ports["alpha"] == "25565"
    # The setup page checks ports as you type, and a port picked by hand that's taken is refused.
    info = c.get("/api/hub/port?port=25565")[1]
    assert info["used_by"] == "Alpha" and info["suggestion"] not in (25565, int(ports["my-world"]), int(ports["my-world-2"]))
    assert c.get("/api/hub/port?port=25590")[1]["used_by"] is None
    assert c.get("/api/hub/port?port=80")[0] == 400
    status, body, _ = c.post("/api/hub/create", {"loader": "vanilla", "motd": "Clash", "port": 25565, "accept_eula": True})
    assert status == 400 and "already used" in body["error"]
    # Two servers can't be given the same port...
    status, body, _ = c.post("/api/servers/my-world/settings", {"port": 25565})
    assert status == 400 and "alpha" in body["error"]
    assert c.post("/api/servers/my-world/settings", {"port": 25600})[0] == 200
    settings = c.get("/api/servers/my-world/settings")[1]
    assert settings["port"] == 25600 and settings["properties"]["view-distance"] == "12"
    assert c.post("/api/servers/my-world/settings", {"properties": {"spawn-protection": 0, "level-seed": "abc"}})[0] == 200
    props = read_properties(d.m.server_dir / "server.properties")
    assert props["spawn-protection"] == "0" and props["level-seed"] == "abc" and props["pvp"] == "false"
    assert c.post("/api/servers/my-world/settings", {"properties": {"rcon.password": "x"}})[0] == 400
    assert c.post("/api/servers/my-world/settings", {"properties": {"view-distance": 99}})[0] == 400
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
    assert "Create your first server" in out
    if cli.has_display():
        assert "Password:       PASSWORD" in out
    else:  # no screen: other devices sign in, with a one-time password until you choose one
        assert "Password:       Mcsm-" in out and "one-time" in out
        assert (home / ".mcsm" / "first-password.txt").is_file()

    # A server folder elsewhere is listed too; a home-folder server that never installed goes to setup.
    other = tmp_path / "other-server"
    setupmod.configure(other, setupmod.SetupSpec.from_dict({"loader": "fabric", "accept_eula": True}))
    setupmod.configure(home, setupmod.SetupSpec.from_dict({"loader": "fabric", "accept_eula": True}))
    assert cli.main(["-C", str(other), "start", "--no-browser"]) == 0
    assert set(ran[-1].discover()) == {"main", "other-server"}
    assert setupmod.is_pending(home)
