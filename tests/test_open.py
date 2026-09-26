"""'Open folder' buttons: only on the server's own computer, only mcsm's own folders."""

from mcsm import opener

from test_hub import login


def test_open_folders(hub_env, monkeypatch):
    hub, c = hub_env
    login(c)
    opened = []
    monkeypatch.setattr(opener, "open_path", lambda p: opened.append(p) or True)
    assert c.get("/api/hub")[1]["local"] is True

    d = hub.get("alpha")
    assert c.post("/api/servers/alpha/open", {"what": "mods"})[0] == 200
    assert opened[-1] == d.m.server_dir / "mods"
    assert c.post("/api/servers/alpha/open", {"what": "backups"})[0] == 200
    assert opened[-1] == d.m.config.backups.dir and opened[-1].is_dir()  # made if missing
    status, body, _ = c.post("/api/servers/alpha/open", {"what": "world"})
    assert status == 404 and "doesn't exist yet" in body["error"]  # never run
    assert c.post("/api/servers/alpha/open", {"what": "/etc"})[0] == 400
    assert c.post("/api/servers/alpha/open", {"what": "../.."})[0] == 400
    assert c.post("/api/hub/open", {"what": "home"})[0] == 200 and opened[-1] == hub.home

    # Through a proxy (or from another device), there's no screen to open it on.
    before = len(opened)
    via_proxy = {"X-Forwarded-For": "203.0.113.9"}
    assert c.post("/api/servers/alpha/open", {"what": "mods"}, headers=via_proxy)[0] == 403
    assert c.post("/api/hub/open", {"what": "home"}, headers=via_proxy)[0] == 403
    assert len(opened) == before
