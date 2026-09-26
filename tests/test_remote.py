"""Remote access: strong passwords only, and phones paired by QR code with limited powers."""

import pytest

from mcsm import qr, webauth
from mcsm.config import ConfigError

from test_hub import login
from test_web import Client

STRONG = "Correct-Horse-9"
AWAY = {"X-Forwarded-For": "203.0.113.9"}  # looks like a request from another device


def test_password_rules():
    assert webauth.strong_password(STRONG)
    for weak in ("short-A1!", "alllowercase-123", "ALLUPPERCASE-123", "NoSpecialChars12"):
        assert not webauth.strong_password(weak)


def test_qr_code_svg():
    svg = qr.svg("http://192.168.1.20:8765/#pair=abc")
    assert svg.startswith("<svg") and "path" in svg
    with pytest.raises(ValueError):
        qr.QrCode("x" * 5000)


def as_other_device(c):
    other = Client(c.base)
    other.call = (lambda f: (lambda m, p, body=None, headers=None, raw=None: f(m, p, body, {**AWAY, **(headers or {})}, raw)))(other.call)
    return other


def test_remote_access_needs_a_strong_password_and_phones_are_limited(hub_env):
    hub, c = hub_env
    login(c)
    assert c.post("/api/hub/network", {"enabled": True})[0] == 400  # still the default password
    assert c.post("/api/auth/change", {"mode": "pin", "secret": "1234"})[0] == 200
    status, body, _ = c.post("/api/hub/network", {"enabled": True})
    assert status == 400 and "strong password" in body["error"]
    away = as_other_device(c)
    assert away.post("/api/login", {"password": "1234"})[0] == 403  # PINs don't work from elsewhere
    assert c.post("/api/auth/change", {"mode": "password", "secret": STRONG})[0] == 200
    assert c.post("/api/hub/network", {"enabled": True})[0] == 200
    assert c.post("/api/auth/change", {"mode": "pin", "secret": "4321"})[0] == 400  # not while it's on
    assert c.post("/api/auth/change", {"mode": "password", "secret": "weakpassword"})[0] == 400
    assert away.post("/api/login", {"password": STRONG})[0] == 200

    # Pairing a phone: a one-time code shown as a QR code.
    ui = c  # the owner's browser
    info = ui.get("/api/hub/remote")[1]
    assert info["strong"] and info["network_access"] and "12 characters" in info["rules"]
    webui = hub.ui
    webui.host = "0.0.0.0"  # as if mcsm had restarted with network access on
    host = info["addresses"][0]["host"] if info["addresses"] else None
    if host is None:  # no network here: pretend there's a home network address
        hub.save_share(hub.share_settings()["port"], "mc.example.com")
        host = "mc.example.com"
    r = ui.post("/api/hub/devices/pair", {"host": host})[1]
    assert r["qr"].startswith("<svg") and "#pair=" in r["url"]
    code = r["url"].split("#pair=")[1]
    phone = as_other_device(Client(c.base))
    assert phone.get("/api/hub")[0] == 401
    status, body, _ = phone.post("/api/pair", {"code": code, "name": "Sam's phone <b>"})
    assert status == 200 and body["name"] == "Sam's phone b"
    assert phone.post("/api/pair", {"code": code, "name": "again"})[0] == 400  # used up
    assert phone.get("/api/hub")[1]["device"] == "Sam's phone b"
    assert phone.get("/api/servers/alpha/status")[0] == 200
    assert phone.post("/api/servers/alpha/backups/create", {"name": "from phone"})[0] == 200
    for path, body in (("/api/servers/alpha/command", {"command": "op someone"}), ("/api/servers/alpha/settings", {}),
                       ("/api/servers/alpha/mods/add", {"id": "x"}), ("/api/hub/network", {"enabled": False}),
                       ("/api/auth/change", {"mode": "password", "secret": STRONG + "x"})):
        assert phone.post(path, body)[0] == 403, path
    assert phone.get("/api/servers/alpha/settings")[0] == 403
    assert phone.get("/api/hub/remote")[0] == 403
    devices = ui.get("/api/hub/remote")[1]["devices"]
    assert [d["name"] for d in devices] == ["Sam's phone b"] and "hash" not in devices[0]

    # Removing it (or changing the password) signs it out.
    assert ui.post("/api/hub/devices/remove", {"id": devices[0]["id"]})[0] == 200
    assert phone.get("/api/hub")[0] == 401
    code = ui.post("/api/hub/devices/pair", {"host": host})[1]["url"].split("#pair=")[1]
    assert phone.post("/api/pair", {"code": code, "name": "phone"})[0] == 200
    assert c.post("/api/auth/change", {"mode": "password", "secret": STRONG + "2"})[0] == 200
    assert phone.get("/api/hub")[0] == 401
    with pytest.raises(ConfigError):
        webui.devices.pair("nope", "x", "1.2.3.4", 0)
