"""Posting a server's invite to a Discord channel through the user's own bot."""

import pytest

from mcsm import discord
from mcsm.discord import API, Discord, DiscordError
from mcsm.http import HttpError

from test_hub import login

TOKEN = "test-" + "x" * 60  # a made-up token (built here so it never looks like a real one)
GUILD, CHANNEL, OTHER = "111111111111111111", "222222222222222222", "333333333333333333"


def fake_discord(http):
    http.json[f"{API}/users/@me"] = {"id": "999999999999999999", "username": "mcsm-bot"}
    http.json[f"{API}/users/@me/guilds"] = [{"id": GUILD, "name": "Our Club"}]
    http.json[f"{API}/guilds/{GUILD}/channels"] = [
        {"id": "444444444444444444", "type": 4, "name": "General stuff", "position": 0},
        {"id": CHANNEL, "type": 0, "name": "minecraft", "parent_id": "444444444444444444", "position": 2},
        {"id": "555555555555555555", "type": 2, "name": "Voice", "position": 1}]
    sent = []
    http.posts[f"{API}/channels/{CHANNEL}/messages"] = lambda body: sent.append(body) or {"id": "777777777777777777"}
    return sent


def test_bot_basics(http):
    fake_discord(http)
    bot = Discord(http, TOKEN)
    assert bot.me() == {"id": "999999999999999999", "name": "mcsm-bot"}
    assert "client_id=999999999999999999" in Discord.invite_url("999999999999999999")
    assert [c["name"] for c in bot.channels(GUILD)] == ["minecraft"]  # text channels only
    assert discord.check_token("Bot " + TOKEN) == TOKEN
    with pytest.raises(DiscordError):
        discord.check_token("hunter2")
    http.json[f"{API}/users/@me"] = HttpError(f"{API}/users/@me", 401, "HTTP 401")
    with pytest.raises(DiscordError, match="token"):
        bot.me()


def test_post_the_invite(hub_env):
    hub, c = hub_env
    login(c)
    sent = fake_discord(hub.http)
    assert c.get("/api/hub/discord")[1]["set"] is False
    assert c.get("/api/hub/discord/guilds")[0] == 400  # no bot yet
    status, body, _ = c.post("/api/hub/discord", {"token": TOKEN})
    assert status == 200 and body["bot"]["name"] == "mcsm-bot" and "client_id=999" in body["invite_url"]
    info = c.get("/api/hub/discord")[1]
    assert info["set"] and "token" not in info and TOKEN not in str(info)  # never handed back
    assert c.get("/api/hub/discord/guilds")[1]["guilds"] == [{"id": GUILD, "name": "Our Club"}]
    assert [x["id"] for x in c.get(f"/api/hub/discord/channels?guild={GUILD}")[1]["channels"]] == [CHANNEL]

    hub.save_share(hub.share_settings()["port"], "mc.example.com")
    assert c.post("/api/servers/alpha/client/discord", {"guild": GUILD, "channel": CHANNEL})[0] == 400  # friends off
    assert c.post("/api/servers/alpha/client", {"enabled": True})[0] == 200
    assert c.post("/api/servers/alpha/client/discord", {"guild": GUILD, "channel": OTHER})[0] == 400  # not in it
    status, body, _ = c.post("/api/servers/alpha/client/discord",
                             {"guild": GUILD, "channel": CHANNEL, "message": "Server's up! @everyone"})
    assert status == 200, body
    msg = sent[-1]
    assert msg["content"] == "Server's up! @everyone" and msg["allowed_mentions"] == {"parse": []}
    assert "http://mc.example.com:" in msg["embeds"][0]["description"]
    assert hub.discord_settings()["channel"] == CHANNEL  # picked again next time
    assert c.post("/api/hub/discord", {"token": ""})[0] == 200 and not hub.discord_settings()["set"]
