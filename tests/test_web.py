"""The web UI's API, served by a real daemon running the fake Minecraft server."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from mcsm.config import ModSpec
from mcsm.daemon import Daemon
from mcsm.players import offline_uuid

from test_manager import manager, update


class Client:
    def __init__(self, base):
        self.base = base
        self.cookie = None

    def call(self, method, path, body=None, headers=None, raw=None):
        h = {"X-MCSM": "1", **(headers or {})}
        if self.cookie:
            h["Cookie"] = self.cookie
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if body is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if "Set-Cookie" in r.headers:
                    self.cookie = r.headers["Set-Cookie"].split(";")[0]
                raw_body = r.read()
                ctype = r.headers.get("Content-Type", "")
                return r.status, json.loads(raw_body) if "json" in ctype else raw_body.decode(), r.headers
        except urllib.error.HTTPError as e:
            raw_body = e.read() or b"{}"
            try:
                return e.code, json.loads(raw_body), e.headers
            except ValueError:
                return e.code, raw_body.decode(), e.headers

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)


def wait_for(fn, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


@pytest.fixture
def running(make_config, http, modrinth):
    yield from _running(make_config, http, modrinth, "hunter2hunter2")


@pytest.fixture
def running_default(make_config, http, modrinth):
    """No password in mcsm.toml: sign-in is chosen in the web UI."""
    yield from _running(make_config, http, modrinth, "")


def _running(make_config, http, modrinth, password):
    modrinth.project("AAA", "goodmod", "Good Mod")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    modrinth.project("BBB", "othermod", "Other Mod")
    modrinth.version("BBB", "1.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "goodmod")])
    cfg.web.port = 0
    cfg.web.password = password
    m = manager(cfg, http, ["1.21.1"])
    assert update(m).ok
    d = Daemon(m, tick=0.1)
    t = threading.Thread(target=d.run, kwargs={"web": True}, daemon=True)
    t.start()
    wait_for(lambda: getattr(d, "ui", None) is not None and d.state == "running")
    client = Client(d.ui.url.rstrip("/"))
    yield d, client, cfg
    d.stop_requested.set()
    t.join(20)


def login(c):
    status, body, _ = c.post("/api/login", {"password": "hunter2hunter2"})
    assert status == 200, body


def test_auth_and_csrf(running):
    d, c, cfg = running
    assert c.get("/api/status")[0] == 401
    assert c.post("/api/login", {"password": "nope"})[0] == 401
    status, _, _ = c.call("POST", "/api/login", {"password": "hunter2hunter2"}, headers={"X-MCSM": ""})
    assert status == 403  # no CSRF header
    login(c)
    assert c.get("/api/status")[0] == 200
    # State-changing calls need the header even with a valid session.
    assert c.call("POST", "/api/server/stop", {}, headers={"X-MCSM": "0"})[0] == 403
    c.post("/api/logout")
    assert c.get("/api/status")[0] == 401


def test_login_is_rate_limited(running):
    _, c, _ = running
    for _ in range(5):
        c.post("/api/login", {"password": "wrong"})
    assert c.post("/api/login", {"password": "hunter2hunter2"})[0] == 429


def test_static_page_and_headers(running):
    _, c, _ = running
    status, body, headers = c.get("/")
    assert status == 200 and "mcsm" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    assert c.get("/app.js")[0] == 200


def test_status_console_and_commands(running):
    d, c, _ = running
    login(c)
    _, s, _ = c.get("/api/status")
    assert s["state"] == "running" and s["minecraft"] == "1.21.1" and s["mods"] == 1
    assert c.post("/api/command", {"command": "/list"})[0] == 200
    wait_for(lambda: any("players online" in line["text"] for line in c.get("/api/console?since=0")[1]["lines"]))
    lines = c.get("/api/console?since=0")[1]["lines"]
    assert any(line["user"] and line["text"] == "> list" for line in lines)
    last = c.get("/api/console?since=0")[1]["last"]
    assert c.get(f"/api/console?since={last}")[1]["lines"] == []


def test_stop_start_and_jobs(running):
    d, c, _ = running
    login(c)
    assert c.post("/api/server/stop")[0] == 200
    wait_for(lambda: c.get("/api/status")[1]["state"] == "stopped" and not c.get("/api/status")[1]["job"])
    time.sleep(0.5)
    assert d.state == "stopped"  # a deliberate stop isn't treated as a crash
    assert c.post("/api/server/start")[0] == 200
    wait_for(lambda: c.get("/api/status")[1]["state"] == "running")
    wait_for(lambda: not c.get("/api/status")[1]["job"])


def test_update_check_and_mod_management(running, modrinth):
    d, c, cfg = running
    login(c)
    assert c.post("/api/updates/check")[0] == 200
    wait_for(lambda: c.get("/api/updates")[1]["check"] is not None)
    check = c.get("/api/updates")[1]["check"]
    assert check["up_to_date"] and check["installed"] == "1.21.1"

    status, body, _ = c.post("/api/mods/add", {"source": "modrinth", "id": "othermod", "required": False})
    assert status == 200, body
    assert c.post("/api/mods/add", {"source": "modrinth", "id": "othermod"})[0] == 409
    assert c.post("/api/mods/add", {"source": "modrinth", "id": "../../etc"})[0] == 400
    configured = c.get("/api/mods")[1]["configured"]
    assert {"source": "modrinth", "id": "othermod", "required": False} in configured

    wait_for(lambda: not c.get("/api/status")[1]["job"])
    assert c.post("/api/updates/apply", {})[0] == 200
    wait_for(lambda: len(d.m.lock.mods) == 2, timeout=30)
    wait_for(lambda: c.get("/api/status")[1]["state"] == "running" and not c.get("/api/status")[1]["job"])

    assert c.post("/api/mods/remove", {"source": "modrinth", "id": "othermod"})[0] == 200
    assert all(s["id"] != "othermod" for s in c.get("/api/mods")[1]["configured"])


def test_settings_validation(running):
    d, c, cfg = running
    login(c)
    before = cfg.path.read_text()
    assert c.post("/api/settings", {"strategy": "bogus"})[0] == 400
    assert cfg.path.read_text() == before  # rolled back
    assert c.post("/api/settings", {"strategy": "mods-only", "auto_upgrade": False,
                                    "warn_minutes": [5, 1], "check_interval": "2h"})[0] == 200
    s = c.get("/api/settings")[1]
    assert s["strategy"] == "mods-only" and s["auto_upgrade"] is False
    assert s["warn_minutes"] == [5, 1] and s["check_interval"] == "2h"
    assert d.m.config.updates.strategy == "mods-only"


def test_backups_and_restore_requires_stop(running):
    d, c, cfg = running
    login(c)
    assert c.post("/api/backups/create", {"label": "web test"})[0] == 200
    wait_for(lambda: any("web_test" in b["name"] for b in c.get("/api/backups")[1]["backups"]))
    name = c.get("/api/backups")[1]["backups"][0]["name"]
    assert c.post("/api/backups/restore", {"name": name})[0] == 409  # still running
    assert c.post("/api/backups/restore", {"name": "../mcsm.toml"})[0] == 404


def test_manual_upload_only_accepts_expected_files(running):
    d, c, cfg = running
    login(c)
    status, body, _ = c.call("POST", "/api/manual/upload?filename=evil.jar", raw=b"x",
                             headers={"Content-Type": "application/octet-stream"})
    assert status == 400
    d.last_check = {"manual": [{"filename": "blocked.jar", "name": "B", "url": "u"}], "target": None,
                    "checked_at": 0, "up_to_date": False, "latest": "1.21.1"}
    status, body, _ = c.call("POST", "/api/manual/upload?filename=blocked.jar", raw=b"jar bytes",
                             headers={"Content-Type": "application/octet-stream"})
    assert status == 200, body
    assert (cfg.manual_dir / "blocked.jar").read_bytes() == b"jar bytes"
    assert d.last_check["manual"] == []


def test_web_players_page(running):
    d, c, cfg = running
    login(c)
    d._on_line("[12:00:00] [Server thread/INFO]: Steve joined the game")
    d._on_line("[12:00:01] [Server thread/INFO]: <Steve> Alex joined the game")  # chat can't fake a join
    status, body, _ = c.get("/api/players")
    assert status == 200 and body["online"] == ["Steve"] and body["running"]

    status, body, _ = c.post("/api/players/action", {"action": "op", "name": "Steve"})
    assert status == 200 and body["message"] == "sent: op Steve"
    lines = c.get("/api/console?since=0")[1]["lines"]
    assert any(line["user"] and line["text"] == "> op Steve" for line in lines)
    assert c.post("/api/players/action", {"action": "op", "name": "a b"})[0] == 400

    # Stopped: edits the files instead (the test config runs with online-mode on, so seed usercache).
    assert c.post("/api/server/stop")[0] == 200
    wait_for(lambda: c.get("/api/status")[1]["state"] == "stopped" and not c.get("/api/status")[1]["job"])
    (cfg.server.dir / "usercache.json").write_text(json.dumps([{"name": "Alex", "uuid": offline_uuid("Alex")}]))
    status, body, _ = c.post("/api/players/action", {"action": "ban", "name": "Alex", "reason": "griefing"})
    assert status == 200, body
    bans = c.get("/api/players")[1]["bans"]
    assert bans[0]["name"] == "Alex" and bans[0]["reason"] == "griefing"
    assert c.post("/api/players/action", {"action": "kick", "name": "Alex"})[0] == 400


def test_default_password_and_changing_it(running_default):
    d, c, cfg = running_default
    assert c.get("/api/auth")[1] == {"mode": "password", "default": True, "managed": False, "local": True}
    assert c.post("/api/login", {"password": "passw0rd"})[0] == 401
    assert c.post("/api/login", {"password": " password "})[0] == 200  # the default ignores case
    assert c.post("/api/login", {"password": "PASSWORD"})[0] == 200
    assert c.get("/api/status")[1]["auth"]["default"] is True  # the UI asks to change it

    other = Client(c.base)
    assert other.post("/api/login", {"password": "PASSWORD"})[0] == 200
    assert c.post("/api/auth/change", {"mode": "password", "secret": "PASSWORD"})[0] == 400
    assert c.post("/api/auth/change", {"mode": "pin", "secret": "12ab"})[0] == 400
    status, body, _ = c.post("/api/auth/change", {"mode": "pin", "secret": "4821"})
    assert status == 200 and body["mode"] == "pin" and not body["default"]
    assert c.get("/api/status")[0] == 200       # this browser stays signed in
    assert other.get("/api/status")[0] == 401   # everyone else is signed out
    assert other.post("/api/login", {"password": "PASSWORD"})[0] == 401
    assert other.post("/api/login", {"password": "4821"})[0] == 200
    stored = (cfg.state_dir / "web-auth.json").read_text()
    assert "4821" not in stored and "PASSWORD" not in stored  # only a salted hash

    # No password: this computer gets in without signing in...
    assert c.post("/api/auth/change", {"mode": "none"})[0] == 200
    assert Client(c.base).get("/api/status")[0] == 200
    assert Client(c.base).post("/api/login", {"password": ""})[0] == 401
    # ...but anything arriving through a proxy doesn't count as this computer.
    assert Client(c.base).call("GET", "/api/status", headers={"X-Forwarded-For": "203.0.113.9"})[0] == 401
    assert c.call("POST", "/api/auth/change", {"mode": "none"}, headers={"X-Forwarded-For": "203.0.113.9"})[0] == 400

    # `mcsm web-password --reset` while running goes back to PASSWORD.
    from mcsm import webauth
    webauth.AuthStore(cfg).reset()
    fresh = Client(c.base)
    assert fresh.get("/api/status")[0] == 401
    assert fresh.post("/api/login", {"password": "PASSWORD"})[0] == 200


def test_foreign_host_names_are_refused(running_default):
    d, c, cfg = running_default
    port = c.base.rsplit(":", 1)[1]
    assert c.call("GET", "/api/auth", headers={"Host": f"evil.example:{port}"})[0] == 421
    assert c.call("GET", "/", headers={"Host": "evil.example"})[0] == 421
    for ok in (f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}", "192.168.1.20", "mypc.local"):
        assert c.call("GET", "/api/auth", headers={"Host": ok})[0] == 200, ok
    cfg.web.allowed_hosts.append("mc.example.com")
    assert c.call("GET", "/api/auth", headers={"Host": "mc.example.com"})[0] == 200
