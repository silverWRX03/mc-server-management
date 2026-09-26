"""Bringing existing worlds to a server: from a .zip or a singleplayer save."""

import gzip
import io
import zipfile

import pytest

from mcsm import backup, nbt, world
from mcsm.nbt import BYTE, Tagged

from test_hub import login
from test_web import wait_for


def level_dat(name="My World", version="1.20.1", hardcore=False) -> bytes:
    return gzip.compress(nbt.dumps({"Data": {"LevelName": name, "Version": {"Name": version},
                                             "hardcore": Tagged(BYTE, int(hardcore))}}))


def zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_level_info():
    assert world.level_info(level_dat(hardcore=True)) == {"name": "My World", "version": "1.20.1", "hardcore": True}
    assert world.level_info(b"not gzip") == {}


def test_install_from_zip(tmp_path):
    src = tmp_path / "w.zip"
    src.write_bytes(zip_bytes({"saves/My World/level.dat": level_dat(), "saves/My World/region/r.0.0.mca": b"r",
                               "saves/My World/session.lock": b"x", "saves/My World/../../evil.txt": b"x",
                               "readme.txt": b"not the world"}))
    info = world.install(src, tmp_path / "server" / "world")
    w = tmp_path / "server" / "world"
    assert info["name"] == "My World" and (w / "region" / "r.0.0.mca").read_bytes() == b"r"
    assert not (w / "session.lock").exists() and not (tmp_path / "evil.txt").exists() and not (w / "readme.txt").exists()
    with pytest.raises(world.WorldError, match="already exists"):
        world.install(src, w)

    # A Paper/Spigot server's world: the Nether and the End sit next to it.
    paper = tmp_path / "paper.zip"
    paper.write_bytes(zip_bytes({"world/level.dat": level_dat(), "world_nether/DIM-1/region/n.mca": b"n",
                                 "world_the_end/DIM1/region/e.mca": b"e"}))
    world.install(paper, tmp_path / "p")
    assert (tmp_path / "p" / "DIM-1" / "region" / "n.mca").read_bytes() == b"n"
    assert (tmp_path / "p" / "DIM1" / "region" / "e.mca").read_bytes() == b"e"

    empty = tmp_path / "empty.zip"
    empty.write_bytes(zip_bytes({"hello.txt": b"hi"}))
    with pytest.raises(world.WorldError, match="no level.dat"):
        world.install(empty, tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_replace_puts_the_old_world_back_on_failure(tmp_path):
    dest = tmp_path / "world"
    dest.mkdir()
    (dest / "level.dat").write_bytes(b"old")
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(world.WorldError):
        world.replace(bad, dest)
    assert (dest / "level.dat").read_bytes() == b"old"
    good = tmp_path / "good.zip"
    good.write_bytes(zip_bytes({"level.dat": level_dat(name="New")}))
    assert world.replace(good, dest)["name"] == "New"
    assert not any(p.name.startswith(".") for p in tmp_path.iterdir())


def singleplayer(tmp_path):
    saves = tmp_path / "mc" / "saves"
    (saves / "Old").mkdir(parents=True)
    (saves / "Old" / "level.dat").write_bytes(level_dat(name="Old Times", version="1.16.5"))
    (saves / "Old" / "region").mkdir()
    (saves / "Old" / "region" / "r.0.0.mca").write_bytes(b"old region")
    (saves / "not a world").mkdir()
    return [("Minecraft Launcher", saves)]


def test_singleplayer_worlds(tmp_path):
    sources = singleplayer(tmp_path)
    worlds = world.list_saves(sources)
    assert [(w["name"], w["version"], w["launcher"]) for w in worlds] == [("Old Times", "1.16.5", "Minecraft Launcher")]
    assert world.find_save(worlds[0]["id"], sources) == sources[0][1] / "Old"
    with pytest.raises(world.WorldError):
        world.find_save("0" * 16, sources)
    world.install(sources[0][1] / "Old", tmp_path / "copy")
    assert (tmp_path / "copy" / "region" / "r.0.0.mca").read_bytes() == b"old region"


def test_worlds_on_the_web(hub_env, tmp_path, monkeypatch):
    hub, c = hub_env
    login(c)
    sources = singleplayer(tmp_path)
    monkeypatch.setattr(world, "_launcher_saves", lambda: sources)
    saves = c.get("/api/hub/saves")[1]["worlds"]
    assert saves[0]["name"] == "Old Times"

    # A new server started from a singleplayer world.
    status, body, _ = c.post("/api/hub/create", {"loader": "vanilla", "motd": "From save", "world": "save:" + saves[0]["id"],
                                                  "accept_eula": True})
    assert status == 200, body
    d = hub.get(body["id"])
    wait_for(lambda: d.last_job and d.last_job["name"] == "set up server", timeout=30)
    assert d.last_job["ok"], d.last_job
    assert (d.m.server_dir / "world" / "region" / "r.0.0.mca").read_bytes() == b"old region"
    assert c.post("/api/hub/create", {"loader": "vanilla", "world": "save:" + "0" * 16, "accept_eula": True})[0] == 400
    assert c.post("/api/hub/create", {"loader": "vanilla", "world": "../x", "accept_eula": True})[0] == 400

    # Replacing the world of an existing server, from an uploaded .zip; the old one is backed up.
    alpha = hub.get("alpha")
    (alpha.m.server_dir / "world").mkdir(exist_ok=True)
    (alpha.m.server_dir / "world" / "level.dat").write_bytes(b"alpha's world")
    staged = c.call("POST", "/api/hub/stage?filename=new.zip", raw=zip_bytes({"New/level.dat": level_dat(name="New")}))[1]
    assert c.post("/api/servers/alpha/world/replace", {"world": staged["id"]})[0] == 200
    wait_for(lambda: alpha.last_job and alpha.last_job["name"] == "replace world", timeout=30)
    assert alpha.last_job["ok"], alpha.last_job
    assert world.level_info((alpha.m.server_dir / "world" / "level.dat").read_bytes())["name"] == "New"
    assert any("before-new-world" in b.name for b in backup.list_backups(alpha.m.config.backups.dir))
    assert c.post("/api/servers/alpha/world/replace", {"world": "nope"})[0] == 400
