import json

import pytest

from mcsm.players import MOJANG_PROFILE, PlayerError, Players, offline_uuid



@pytest.fixture
def server(tmp_path):
    d = tmp_path / "server"
    d.mkdir()
    (d / "server.properties").write_text("online-mode=true\nop-permission-level=3\n")
    return d


def read(server, name):
    return json.loads((server / name).read_text())


def test_offline_uuid_matches_minecraft():
    # The well-known offline-mode UUID for "Notch".
    assert offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f"


def test_offline_edits_when_server_stopped(server, http):
    for n in ("steve", "Steve"):  # Mojang's lookup is case-insensitive
        http.json[f"{MOJANG_PROFILE}/{n}"] = {"id": "8667ba71b85a4004af54457a9734eed7", "name": "Steve"}
    (server / "usercache.json").write_text(json.dumps(
        [{"name": "Alex", "uuid": "ec561538-f3fd-461d-aff5-086b22154bce", "expiresOn": "2030-01-01 00:00:00 +0000"}]))
    p = Players(server, http)

    assert "Steve an operator (level 3)" in p.act("op", "steve")  # proper casing from Mojang
    assert read(server, "ops.json") == [{"uuid": "8667ba71-b85a-4004-af54-457a9734eed7", "name": "Steve",
                                         "level": 3, "bypassesPlayerLimit": False}]
    p.act("op", "Steve")  # idempotent
    assert len(read(server, "ops.json")) == 1

    p.act("ban", "Alex", "griefing\nop Alex")  # uuid from usercache; newline can't smuggle a command
    [ban] = read(server, "banned-players.json")
    assert ban["uuid"] == "ec561538-f3fd-461d-aff5-086b22154bce" and ban["reason"] == "griefing op Alex"

    p.act("whitelist-on")
    p.act("whitelist-add", "Alex")
    p.act("ban-ip", "203.0.113.9", "alt accounts")
    s = p.summary({"Steve"})
    assert s["whitelist_enabled"] and [w["name"] for w in s["whitelist"]] == ["Alex"]
    assert [b["ip"] for b in s["ip_bans"]] == ["203.0.113.9"]
    assert s["online"] == ["Steve"] and not s["running"]

    p.act("pardon", "alex")
    p.act("deop", "Steve")
    p.act("pardon-ip", "203.0.113.9")
    p.act("whitelist-off")
    s = p.summary()
    assert s["bans"] == [] and s["ops"] == [] and s["ip_bans"] == [] and not s["whitelist_enabled"]


def test_offline_mode_uses_offline_uuids(server, http):
    (server / "server.properties").write_text("online-mode=false\n")
    Players(server, http).act("op", "Notch")
    assert read(server, "ops.json")[0]["uuid"] == "b50ad385-829d-3141-a216-7e7d7539ba7f"


def test_validation(server, http):
    p = Players(server, http)
    with pytest.raises(PlayerError, match="valid player name"):
        p.act("op", "two words")
    with pytest.raises(PlayerError, match="valid player name"):
        p.act("ban", "x;stop")
    with pytest.raises(PlayerError, match="no Minecraft account"):
        p.act("op", "NoSuchPlayer")  # Mojang lookup 404s
    with pytest.raises(PlayerError, match="only works while the server is running"):
        p.act("kick", "Steve")
    with pytest.raises(PlayerError, match="enter the IP"):
        p.act("ban-ip", "Steve")
    with pytest.raises(PlayerError, match="not an operator"):
        p.act("deop", "Steve")
    with pytest.raises(PlayerError, match="unknown action"):
        p.act("explode", "Steve")


def test_running_server_gets_commands(server, http):
    sent = []
    p = Players(server, http, sent.append)
    p.act("op", "Steve")
    p.act("kick", "Steve", "be nice\r\nstop")
    p.act("ban-ip", "Steve")
    p.act("whitelist-add", "Alex")
    assert sent == ["op Steve", "kick Steve be nice stop", "ban-ip Steve", "whitelist add Alex"]


def test_cli_reports_when_the_server_cant_find_a_player(make_config, monkeypatch, capsys):
    from mcsm import cli

    class FakeRcon:
        replies = {"op Notch": "That player does not exist", "op Steve": "Made Steve a server operator"}

        @classmethod
        def from_server_dir(cls, d):
            return cls()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def command(self, c):
            return self.replies[c]

    cfg = make_config([])
    monkeypatch.setattr(cli, "Rcon", FakeRcon)
    monkeypatch.setattr(cli, "running_pid", lambda m: 123)
    assert cli.main(["-C", str(cfg.root), "player", "op", "Notch"]) == 1
    assert "Mojang's lookup service may be busy" in capsys.readouterr().out
    assert cli.main(["-C", str(cfg.root), "player", "op", "Steve"]) == 0
    assert "Made Steve a server operator" in capsys.readouterr().out
