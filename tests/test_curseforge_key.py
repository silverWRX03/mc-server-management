"""A CurseForge API key saved in mcsm settings, checked with CurseForge and used by every server."""

import os

from mcsm.http import HttpError
from mcsm.mods import curseforge as cf

from test_hub import login

KEY = "$2a$10$abcdefghijklmnopqrstuv"


def test_save_check_and_remove_the_key(hub_env, monkeypatch):
    hub, c = hub_env
    login(c)
    monkeypatch.delenv("MCSM_CURSEFORGE_API_KEY", raising=False)
    assert c.get("/api/hub/curseforge")[1] == {"set": False}
    url = f"{cf.API}/games/{cf.MINECRAFT_GAME_ID}"
    hub.http.json[url] = HttpError(url, 403, "HTTP 403")
    status, body, _ = c.post("/api/hub/curseforge", {"key": KEY})
    assert status == 400 and "didn't accept" in body["error"]
    assert c.post("/api/hub/curseforge", {"key": "short"})[0] == 400
    hub.http.json[url] = {"data": {"id": 432}}
    assert c.post("/api/hub/curseforge", {"key": KEY})[1]["set"]
    assert os.environ["MCSM_CURSEFORGE_API_KEY"] == KEY
    assert hub.get("alpha").m.config.curseforge_api_key == KEY  # servers pick it up
    assert c.get("/api/hub/curseforge")[1] == {"set": True}
    assert "curseforge_api_key" in hub._hub_file()
    assert c.post("/api/hub/curseforge", {"key": ""})[1] == {"ok": True, "set": False}
    assert "MCSM_CURSEFORGE_API_KEY" not in os.environ
