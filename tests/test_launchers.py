"""Adding a server to Prism, the Modrinth App and CurseForge, and the friend's page (mcfui)."""

import hashlib
import json
import urllib.request
import zipfile

import pytest

from mcsm import join, joinui, launchers, nbt

from test_friends import pack
from test_web import Client, wait_for

JAR = b"mod bytes"
CF_JAR = b"cf mod bytes"


def mods():
    return [
        {"name": "A", "filename": "a.jar", "url": "https://cdn.modrinth.com/data/A/versions/1/a.jar",
         "sha1": hashlib.sha1(JAR).hexdigest(), "side": "both"},
        {"name": "C", "filename": "c.jar", "url": "https://edge.forgecdn.net/files/1/2/c.jar",
         "sha1": hashlib.sha1(CF_JAR).hexdigest(), "side": "client"},
    ]


@pytest.fixture
def joiner(tmp_path, http):
    http.files[mods()[0]["url"]] = JAR
    http.files[mods()[1]["url"]] = CF_JAR
    return join.Joiner(join.Invite("mc.example.com", 8766, "A" * 24), mc_dir=tmp_path / ".minecraft", http=http,
                       say=lambda s: None)


def test_prism_instance(tmp_path, joiner):
    prism = tmp_path / "PrismLauncher"
    r = launchers.install_prism(joiner, pack(mods=mods()), "weekend-survival", prism)
    inst = prism / "instances" / "mcsm-weekend-survival"
    assert r["downloaded"] == 2 and (inst / ".minecraft" / "mods" / "a.jar").read_bytes() == JAR
    components = json.loads((inst / "mmc-pack.json").read_text())["components"]
    assert [(c["uid"], c["version"]) for c in components] == [
        ("net.minecraft", "1.21.1"), ("net.fabricmc.intermediary", "1.21.1"), ("net.fabricmc.fabric-loader", "0.16.5")]
    cfg = (inst / "instance.cfg").read_text()
    assert "name=Weekend Survival" in cfg and "JoinServerOnLaunchAddress=mc.example.com" in cfg and "MaxMemAlloc=4096" in cfg
    assert nbt.loads((inst / ".minecraft" / "servers.dat").read_bytes())["servers"][0]["ip"] == "mc.example.com"
    # Running it again keeps settings changed in Prism, and follows the server's changes.
    (inst / "instance.cfg").write_text(cfg.replace("iconKey=default", "iconKey=creeper"))
    launchers.install_prism(joiner, pack(mods=[], loader="neoforge", loader_version="21.1.77"), "weekend-survival", prism)
    assert "iconKey=creeper" in (inst / "instance.cfg").read_text()
    assert json.loads((inst / "mmc-pack.json").read_text())["components"][1] == {"uid": "net.neoforged", "version": "21.1.77"}
    assert not (inst / ".minecraft" / "mods" / "a.jar").exists()


def test_modrinth_pack(tmp_path, joiner):
    path = launchers.build_mrpack(joiner, pack(mods=mods()), tmp_path)
    assert path.name == "Weekend Survival.mrpack"
    with zipfile.ZipFile(path) as z:
        index = json.loads(z.read("modrinth.index.json"))
        assert index["dependencies"] == {"minecraft": "1.21.1", "fabric-loader": "0.16.5"}
        assert [f["path"] for f in index["files"]] == ["mods/a.jar"]  # on Modrinth's CDN: by link
        f = index["files"][0]
        assert f["fileSize"] == len(JAR) and f["hashes"]["sha512"] == hashlib.sha512(JAR).hexdigest()
        assert z.read("overrides/mods/c.jar") == CF_JAR  # elsewhere: inside the pack
        assert nbt.loads(z.read("overrides/servers.dat"))["servers"][0]["name"] == "Weekend Survival"
    assert launchers.build_mrpack(joiner, pack(), tmp_path).name == "Weekend Survival (2).mrpack"


def test_curseforge_pack(tmp_path, joiner):
    path = launchers.build_curseforge(joiner, pack(mods=mods(), loader="forge", loader_version="47.3.0"), tmp_path)
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["minecraft"] == {"version": "1.21.1", "modLoaders": [{"id": "forge-47.3.0", "primary": True}]}
        assert manifest["manifestType"] == "minecraftModpack" and manifest["overrides"] == "overrides"
        assert z.read("overrides/mods/a.jar") == JAR and z.read("overrides/mods/c.jar") == CF_JAR


def test_tampered_mod_is_refused(tmp_path, joiner, http):
    http.files[mods()[0]["url"]] = b"evil"
    with pytest.raises(join.JoinError, match="checksum"):
        launchers.build_curseforge(joiner, pack(mods=mods()), tmp_path)


def test_run_targets_keeps_going(tmp_path, joiner, monkeypatch):
    monkeypatch.setattr(launchers, "prism_dirs", lambda: [tmp_path / "no-prism"])
    results = joiner.run_targets(pack(mods=mods()), ["curseforge", "minecraft", "prism", "modrinth"],
                                 open_after=False, out_dir=tmp_path)
    by = {r["launcher"]: r for r in results}
    assert [r["launcher"] for r in results] == ["minecraft", "prism", "modrinth", "curseforge"]
    assert not by["minecraft"]["ok"] and "Minecraft Launcher" in by["minecraft"]["message"]  # not installed
    assert not by["prism"]["ok"] and by["modrinth"]["ok"] and by["curseforge"]["ok"]
    assert (tmp_path / "Weekend Survival.mrpack").exists()


def test_detect(tmp_path, monkeypatch):
    mc = tmp_path / ".minecraft"
    mc.mkdir()
    (mc / "launcher_profiles.json").write_text("{}")
    (tmp_path / "prism" / "instances").mkdir(parents=True)
    found = {f.key: f for f in launchers.detect(mc, tmp_path / "prism")}
    assert found["minecraft"].found and found["prism"].found and set(found) == set(launchers.KEYS)


def test_friend_page(tmp_path, http, joiner, monkeypatch):
    http.json["http://mc.example.com:8766/join/" + "A" * 24 + "/pack.json"] = pack(mods=mods())
    monkeypatch.setattr(launchers, "open_prism", lambda slug, address: True)
    ui = joinui.JoinUI(join.Invite("mc.example.com", 8766, "A" * 24), mc_dir=tmp_path / ".minecraft", http=http,
                       prism_dir=tmp_path / "prism", out_dir=tmp_path)
    (tmp_path / "prism" / "instances").mkdir(parents=True)
    url = ui.start()
    try:
        c = Client(url.rstrip("/"))
        status, page, _ = c.get("/")
        assert status == 200 and "join.js" in page
        info = c.get("/api/info")[1]
        assert info["pack"]["name"] == "Weekend Survival" and info["pack"]["mods"] == ["A", "C"]
        assert {x["key"]: x["found"] for x in info["launchers"]}["prism"]
        # Without the secret, from another site, or without the header: refused.
        assert Client(url.split("/", 3)[0] + "//" + url.split("/")[2]).get("/api/info")[0] == 404
        assert c.call("GET", "/api/info", headers={"Host": "evil.example:80"})[0] == 403
        req = urllib.request.Request(url + "api/setup", data=b'{"launchers":["prism"]}', method="POST")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=5)
        assert c.post("/api/setup", {"launchers": []})[0] == 400

        assert info["pack"]["memory_gb"] == 4 and "system_gb" in info
        assert c.post("/api/setup", {"launchers": ["prism"], "memory_gb": 999})[0] == 400
        assert c.post("/api/setup", {"launchers": ["prism", "modrinth"], "memory_gb": 6})[0] == 200  # their own choice
        wait_for(lambda: not ui.running and ui.results, timeout=20)
        cfg = (tmp_path / "prism" / "instances" / "mcsm-weekend-survival" / "instance.cfg").read_text()
        assert "MaxMemAlloc=6144" in cfg
        progress = c.get("/api/progress?since=0")[1]
        assert [r["ok"] for r in progress["results"]] == [True, True] and "Finished." in progress["lines"]
        assert (tmp_path / "prism" / "instances" / "mcsm-weekend-survival" / "mmc-pack.json").exists()
        assert c.post("/api/open", {"launcher": "prism"})[1]["ok"]
        assert c.post("/api/quit")[0] == 200 and ui.done.is_set()
    finally:
        ui.stop()


def test_friend_page_when_the_server_is_away(tmp_path, http):
    ui = joinui.JoinUI(join.Invite("mc.example.com", 8766, "B" * 24), mc_dir=tmp_path, http=http)
    info = ui.info()
    assert info["pack"] is None and "new one" in info["error"]  # the invite isn't known there
    with pytest.raises(ValueError):
        ui.setup(["minecraft"])


def test_join_opens_the_page_or_falls_back(monkeypatch, capsys):
    from mcsm import cli
    code = "A" * 24
    monkeypatch.setattr(cli, "interactive", lambda: True)
    calls = []
    monkeypatch.setattr(joinui, "run", lambda invite, **kw: calls.append("page") or None)  # no browser
    monkeypatch.setattr(join, "run_interactive", lambda invite, **kw: calls.append(kw["targets"]) or 0)
    assert cli.main(["join", f"http://mc.example.com:8766/join/{code}"]) == 0
    assert calls == ["page", ["minecraft"]]
    calls.clear()
    assert cli.main(["join", f"http://mc.example.com:8766/join/{code}", "--launcher", "prism,modrinth"]) == 0
    assert calls == [["prism", "modrinth"]]
    assert cli.main(["join", f"http://mc.example.com:8766/join/{code}", "--launcher", "tlauncher"]) == 2
