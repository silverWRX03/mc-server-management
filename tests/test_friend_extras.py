"""A friend's own shaders, resource packs and mods, and what happens when the server updates."""

import json

from mcsm import friendextras, join, joinui
from mcsm.mods.modrinth import API

from test_friends import pack
from test_web import Client, wait_for


def publish(modrinth):
    modrinth.project("SOD", "sodium", "Sodium", server_side="unsupported")
    modrinth.version("SOD", "0.6", ["1.21.1", "1.21.2"])
    modrinth.project("IRI", "iris", "Iris", server_side="unsupported")
    modrinth.version("IRI", "1.8", ["1.21.1", "1.21.2"], deps=["SOD"])
    modrinth.project("MAP", "minimap", "Mini Map", server_side="unsupported")
    modrinth.version("MAP", "2.0", ["1.21.1"])
    modrinth.project("FAN", "faithful", "Faithful")
    modrinth.version("FAN", "1.0", ["1.21.1"], loaders=("minecraft",), filename="Faithful.zip")
    modrinth.project("BSL", "bsl", "BSL Shaders")
    modrinth.version("BSL", "8.2", ["1.21.1", "1.21.2"], loaders=("iris", "optifine"), filename="BSL.zip")


def test_extras_resolve_with_what_they_need(tmp_path, http, modrinth):
    publish(modrinth)
    store = friendextras.Store(tmp_path, "Weekend Server")
    for kind, pid, name in (("shader", "BSL", "BSL Shaders"), ("resourcepack", "FAN", "Faithful"), ("mod", "MAP", "Mini Map")):
        store.add(kind, pid, pid.lower(), name)
    entries, problems = friendextras.resolve(http, store.load(), pack(), set())
    assert problems == []
    got = {e["name"]: (e["folder"], e.get("needed_by")) for e in entries}
    assert got == {"BSL Shaders": ("shaderpacks", None), "Faithful": ("resourcepacks", None), "Mini Map": ("mods", None),
                   "Iris": ("mods", "your shaders"), "Sodium": ("mods", "Iris")}

    # Into the game: files in their folders, the pack and the shader switched on.
    game = tmp_path / "game"
    j = join.Joiner(join.Invite("mc.example.com", 1, "A" * 24), mc_dir=tmp_path / "mc", http=http, say=lambda s: None)
    j.sync_mods(pack(mods=entries), game)
    assert (game / "shaderpacks" / "BSL.zip").is_file() and (game / "resourcepacks" / "Faithful.zip").is_file()
    assert (game / "mods" / "MAP-2.0.jar").is_file()
    assert 'resourcePacks:["vanilla","file/Faithful.zip"]' in (game / "options.txt").read_text()
    iris = (game / "config" / "iris.properties").read_text()
    assert "shaderPack=BSL.zip" in iris and "enableShaders=true" in iris

    # Removing an extra removes its file next time.
    store.remove("FAN")
    entries, _ = friendextras.resolve(http, store.load(), pack(), set())
    j.sync_mods(pack(mods=entries), game)
    assert not (game / "resourcepacks" / "Faithful.zip").exists()
    assert "file/Faithful.zip" not in (game / "options.txt").read_text()


def test_the_server_moves_to_a_new_minecraft(tmp_path, http, modrinth):
    publish(modrinth)
    store = friendextras.Store(tmp_path, "Weekend Server")
    store.add("resourcepack", "FAN", "faithful", "Faithful")
    store.add("mod", "MAP", "minimap", "Mini Map")
    data = store.load()
    entries, problems = friendextras.resolve(http, data, pack(), set())
    store.save({**data, "minecraft": "1.21.1"})  # installed once, remembering the files
    new = pack(minecraft="1.21.2")
    entries, problems = friendextras.resolve(http, store.load(), new, set())
    changes = friendextras.changes_for(store.load(), new, problems)
    assert {(c["name"], c["action"]) for c in changes} == {("Faithful", "switched off"), ("Mini Map", "removed")}
    data, notes = friendextras.apply_changes(store, store.load(), new, problems)
    assert [i["name"] for i in data["items"]] == ["Faithful"] and data["items"][0]["enabled"] is False
    assert any("switched off" in n for n in notes) and any("removed" in n for n in notes)
    entries, _ = friendextras.resolve(http, data, new, set())
    kept = next(e for e in entries if e["name"] == "Faithful")
    assert kept["enabled"] is False and kept["filename"] == "Faithful.zip"  # the old file, switched off


def test_friend_page_extras(tmp_path, http, modrinth):
    publish(modrinth)
    http.json[f"{API}/search"] = {"hits": [{"project_id": "BSL", "slug": "bsl", "title": "BSL Shaders", "description": "", "downloads": 1}]}
    http.json["http://mc.example.com:8766/join/" + "C" * 24 + "/pack.json"] = pack(name="Weekend Server", minecraft="1.21.2")
    ui = joinui.JoinUI(join.Invite("mc.example.com", 8766, "C" * 24), mc_dir=tmp_path / ".minecraft", http=http,
                       prism_dir=tmp_path / "prism", out_dir=tmp_path)
    (tmp_path / "prism" / "instances").mkdir(parents=True)
    url = ui.start()
    try:
        c = Client(url.rstrip("/"))
        c.get("/api/info")
        assert c.get("/api/extras")[1]["kinds"] == {"shader": True, "resourcepack": True, "mod": True}
        assert c.get("/api/extras/search?kind=shader&q=bsl")[1]["results"][0]["name"] == "BSL Shaders"
        assert c.get("/api/extras/search?kind=nope")[0] == 400
        r = c.post("/api/extras/add", {"kind": "shader", "id": "BSL", "slug": "bsl", "name": "BSL Shaders"})[1]
        assert {a["name"] for a in r["adds"]} == {"Iris", "Sodium"}
        assert c.post("/api/extras/add", {"kind": "mod", "id": "MAP", "slug": "minimap", "name": "Mini Map"})[0] == 200
        # Mini Map has no build for 1.21.2: ask first.
        status, body, _ = c.post("/api/setup", {"launchers": ["prism"]})
        assert status == 409 and [x["name"] for x in body["changes"]] == ["Mini Map"]
        assert c.post("/api/setup", {"launchers": ["prism"], "accept_changes": True})[0] == 200
        wait_for(lambda: not ui.running and ui.results, timeout=20)
        game = tmp_path / "prism" / "instances" / "mcsm-weekend-server" / ".minecraft"
        assert (game / "shaderpacks" / "BSL.zip").is_file() and (game / "mods" / "IRI-1.8.jar").is_file()
        assert [i["name"] for i in c.get("/api/extras")[1]["items"]] == ["BSL Shaders"]
        saved = json.loads((tmp_path / ".minecraft" / "mcsm" / "extras" / "weekend-server.json").read_text())
        assert saved["minecraft"] == "1.21.2"
    finally:
        ui.stop()
