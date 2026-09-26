"""Friends at home and friends elsewhere: a local link and an internet link."""

from types import SimpleNamespace

import pytest

from mcsm import share
from mcsm.http import HttpError

from test_hub import login


def test_public_ip_lookup(hub_env):
    hub, c = hub_env
    login(c)
    url = hub.PUBLIC_IP_SERVICES[0]
    hub.http.json[url] = HttpError(url, 503, "down")
    hub.http.json[hub.PUBLIC_IP_SERVICES[1]] = {"ip": "10.0.0.5"}   # not a public address: ignored
    hub.http.json[hub.PUBLIC_IP_SERVICES[2]] = {"ip": "93.184.216.34"}
    with pytest.raises(RuntimeError):
        hub.PUBLIC_IP_SERVICES = hub.PUBLIC_IP_SERVICES[:2]
        hub.public_ip()
    del hub.PUBLIC_IP_SERVICES
    status, body, _ = c.post("/api/hub/share/public-ip")
    assert status == 200 and body["ip"] == "93.184.216.34" and body["share"]["address"] == "93.184.216.34"

    # A server with friend downloads on gets both links.
    assert c.post("/api/servers/alpha/client", {"enabled": True})[0] == 200
    links = c.get("/api/servers/alpha/client")[1]["links"]
    assert links["internet"].startswith("http://93.184.216.34:")
    assert links["local"] is None or "/join/" in links["local"]


def test_friends_on_the_same_network_join_through_it():
    hub = SimpleNamespace(share_settings=lambda: {"address": "93.184.216.34"})
    server = SimpleNamespace(hub=hub)
    d = SimpleNamespace(m=SimpleNamespace(server_dir=__import__("pathlib").Path("/nonexistent")))
    address = share.ShareServer.address
    assert address(server, d, "192.168.1.20") == "192.168.1.20"
    assert address(server, d, "93.184.216.34") == "93.184.216.34"
    assert address(server, d, "mc.example.com") == "93.184.216.34"
    assert share._is_local("localhost") and not share._is_local("8.8.8.8")
