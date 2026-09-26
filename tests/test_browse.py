"""The mod browser window (search, project pages), modpacks, and adding mods from files."""

import hashlib
import io
import json
import zipfile

import pytest

from mcsm import config as configmod, lock as lockmod, modpack, setup as setupmod
from mcsm.browse import Browser, BrowseError
from mcsm.mods.modrinth import API

from test_hub import login
from test_web import wait_for


def test_search_is_normalised_and_filtered(http):
    seen = []
    http.json[f"{API}/search"] = {"total_hits": 45, "hits": [{
        "project_id": "AAA", "slug": "goodmod", "title": "Good Mod", "description": "Makes things good",
        "icon_url": "https://cdn.modrinth.com/icon.png", "author": "someone", "downloads": 1200, "follows": 5,
        "date_modified": "2026-01-02", "date_created": "2025-01-01", "display_categories": ["utility"],
        "versions": ["1.20.1", "1.21.1"]}]}
    orig = http.get_json
    http.get_json = lambda url, params=None, headers=None: seen.append(params) or orig(url, params, headers)
    b = Browser(http)
    r = b.search("modrinth", "mod", "good", loader="quilt", version="1.21.1", category="utility", sort="downloads", offset=20)
    assert r["total"] == 45 and r["offset"] == 20
    hit = r["results"][0]
    assert hit["name"] == "Good Mod" and hit["url"] == "https://modrinth.com/mod/goodmod" and hit["downloads"] == 1200
    facets = json.loads(seen[-1]["facets"])
    assert ["project_type:mod"] in facets and ["versions:1.21.1"] in facets and ["categories:utility"] in facets
    assert ["categories:quilt", "categories:fabric"] in facets  # Quilt runs Fabric mods
    assert ["server_side:required", "server_side:optional"] in facets  # no client-only mods
    assert seen[-1]["index"] == "downloads" and seen[-1]["offset"] == 20

    with pytest.raises(BrowseError):
        b.search(sort="random")
    with pytest.raises(BrowseError, match="API key"):
        b.search("curseforge", "mod", "x")
    with pytest.raises(BrowseError, match="modpacks"):
        Browser(http, "key").search("curseforge", "modpack", "x")
    assert b.sources == ["modrinth"] and Browser(http, "key").sources == ["modrinth", "curseforge"]


def test_project_page_and_categories(http):
    http.json[f"{API}/project/PACK"] = {
        "id": "PACK", "slug": "coolpack", "title": "Cool Pack", "project_type": "modpack", "body": "# Hi",
        "gallery": [{"url": "https://cdn.modrinth.com/g.png", "title": "shot"}],
        "source_url": "https://github.com/x/y", "discord_url": "javascript:alert(1)", "license": {"name": "MIT"}}
    http.json[f"{API}/project/PACK/version"] = [{"id": "V2abcdef", "version_number": "2.0", "game_versions": ["1.21.1"],
                                                 "loaders": ["fabric"], "date_published": "2026-01-01"}]
    p = Browser(http).project("modrinth", "PACK")
    assert p["kind"] == "modpack" and p["body_format"] == "markdown" and p["license"] == "MIT"
    assert p["links"] == {"source_url": "https://github.com/x/y"}  # only https links
    assert p["versions"][0] == {"id": "V2abcdef", "name": "2.0", "minecraft": ["1.21.1"], "loaders": ["fabric"],
                                "date": "2026-01-01"}
    assert p["url"] == "https://modrinth.com/modpack/coolpack"

    http.json[f"{API}/tag/category"] = [
        {"name": "world-generation", "project_type": "mod", "header": "categories"},
        {"name": "fabric", "project_type": "mod", "header": "loaders"},
        {"name": "adventure", "project_type": "modpack", "header": "categories"}]
    assert Browser(http).categories("modrinth", "mod") == [{"id": "world-generation", "name": "World generation"}]


def make_pack(files, overrides=None, deps=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("modrinth.index.json", json.dumps({
            "formatVersion": 1, "game": "minecraft", "name": "Test Pack", "versionId": "1.0",
            "dependencies": deps or {"minecraft": "1.21.1", "fabric-loader": "0.16.0"}, "files": files}))
        for name, data in (overrides or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def test_modpack_is_applied_safely(tmp_path, http):
    root = tmp_path / "srv"
    setupmod.configure(root, setupmod.SetupSpec.from_dict({"loader": "vanilla", "accept_eula": True}))
    script = b"#!/bin/sh\necho hi\n"
    http.files["https://github.com/x/y/raw/start.sh"] = script
    cdn = "https://cdn.modrinth.com/data/{}/versions/{}/{}"
    pack = tmp_path / "p.mrpack"
    pack.write_bytes(make_pack([
        {"path": "mods/good.jar", "downloads": [cdn.format("AAAAAAAA", "V1111111", "good.jar")],
         "hashes": {"sha1": "0" * 40}, "env": {"server": "required", "client": "required"}},
        {"path": "mods/shaders.jar", "downloads": [cdn.format("BBBBBBBB", "V2222222", "shaders.jar")],
         "hashes": {"sha1": "0" * 40}, "env": {"server": "unsupported", "client": "required"}},
        {"path": "scripts/start.sh", "downloads": ["https://github.com/x/y/raw/start.sh"],
         "hashes": {"sha1": hashlib.sha1(script).hexdigest()}},
        {"path": "../evil.jar", "downloads": ["https://github.com/x/y/raw/start.sh"],
         "hashes": {"sha1": hashlib.sha1(script).hexdigest()}},
        {"path": "mods/elsewhere.jar", "downloads": ["https://evil.example/elsewhere.jar"], "hashes": {"sha1": "1" * 40}},
    ], overrides={"overrides/config/a.txt": "client", "server-overrides/config/a.txt": "server",
                  "overrides/config/b.txt": "b", "overrides/../../escape.txt": "no"}))

    r = modpack.apply_file(root, pack, http, name="Test Pack 1.0")
    assert r["minecraft"] == "1.21.1" and r["loader"] == "fabric" and r["mods"] == 1 and r["client_only"] == 1
    cfg = configmod.load(root)
    assert cfg.server.loader == "fabric" and cfg.server.minecraft == "1.21.1" and cfg.updates.strategy == "mods-only"
    assert [(m.source, m.id) for m in cfg.mods] == [("modrinth", "AAAAAAAA")]
    server = cfg.server.dir
    assert (server / "scripts" / "start.sh").read_bytes() == script
    assert (server / "config" / "a.txt").read_text() == "server"  # server-overrides win
    assert (server / "config" / "b.txt").read_text() == "b"
    assert not (tmp_path / "evil.jar").exists() and not (tmp_path / "escape.txt").exists()
    assert not (server / "mods" / "elsewhere.jar").exists()  # only from trusted hosts

    bad = tmp_path / "bad.mrpack"
    bad.write_bytes(make_pack([], deps={"minecraft": "latest; rm -rf"}))
    with pytest.raises(modpack.ModpackError):
        modpack.apply_file(root, bad, http)
    notapack = tmp_path / "x.zip"
    with zipfile.ZipFile(notapack, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(modpack.ModpackError, match="isn't a Modrinth modpack"):
        modpack.apply_file(root, notapack, http)
    with pytest.raises(modpack.ModpackError):
        modpack.version_info(http, "../../x")


def test_setup_accepts_curseforge_mods(tmp_path):
    spec = setupmod.SetupSpec.from_dict({"loader": "neoforge", "mods": ["curseforge:238222", "jei"], "accept_eula": True})
    cfg = setupmod.configure(tmp_path / "s", spec)
    assert {(m.source, m.id) for m in cfg.mods} == {("curseforge", "238222"), ("modrinth", "jei")}
    with pytest.raises(configmod.ConfigError):
        setupmod.SetupSpec.from_dict({"loader": "fabric", "mods": ["curseforge:abc"], "accept_eula": True})
    with pytest.raises(configmod.ConfigError):
        setupmod.SetupSpec.from_dict({"loader": "fabric", "local_mods": ["../../etc"], "accept_eula": True})
    with pytest.raises(configmod.ConfigError):
        setupmod.SetupSpec.from_dict({"loader": "vanilla", "local_mods": ["0123456789abcdef"], "accept_eula": True})


def test_browse_and_add_from_the_web(hub_env):
    hub, c = hub_env
    login(c)
    hub.http.json[f"{API}/search"] = {"total_hits": 1, "hits": [{"project_id": "AAA", "slug": "goodmod", "title": "Good Mod"}]}
    r = c.get("/api/hub/browse/search?type=mod&q=good&loader=fabric&version=")[1]
    assert r["results"][0]["id"] == "AAA"
    assert c.get("/api/hub/browse/search?type=mod&sort=nope")[0] == 400
    assert c.get("/api/hub/browse/project?id=../x")[0] == 400
    assert c.get("/api/servers/alpha/browse/search?type=mod")[0] == 200
    assert c.get("/api/servers/alpha/mods")[1]["minecraft"] == "1.21.1"

    # Several mods ticked in the browser, added at once; ones already listed are skipped.
    status, body, _ = c.post("/api/servers/alpha/mods/add-many", {"mods": [
        {"source": "modrinth", "id": "goodmod", "name": "Good Mod"},
        {"source": "modrinth", "id": "goodmod", "name": "Good Mod"}]})
    assert status == 200 and body["added"] == ["Good Mod"] and body["skipped"][0]["name"] == "Good Mod"
    assert c.post("/api/servers/alpha/mods/add-many", {"mods": []})[0] == 400

    # A jar from this computer: Modrinth knows it, so it becomes a mod mcsm keeps up to date...
    mods_dir = hub.get("alpha").m.server_dir / "mods"
    hub.http.posts[f"{API}/version_files"] = lambda body: {body["hashes"][0]: {"project_id": "FAPI"}} \
        if body["hashes"][0] == hashlib.sha1(b"known").hexdigest() else {}
    status, body, _ = c.call("POST", "/api/servers/alpha/mods/local?filename=fabric-api-0.1.jar", raw=b"known")
    assert status == 200 and body["managed"], body
    assert not (mods_dir / "fabric-api-0.1.jar").exists()
    # ...or it stays as the admin's own file.
    status, body, _ = c.call("POST", "/api/servers/alpha/mods/local?filename=my-mod%2B1.0.jar", raw=b"homemade")
    assert status == 200 and not body["managed"]
    assert (mods_dir / "my-mod+1.0.jar").read_bytes() == b"homemade"
    assert c.call("POST", "/api/servers/alpha/mods/local?filename=evil.exe", raw=b"x")[0] == 400
    assert c.call("POST", "/api/servers/alpha/mods/local?filename=../x.jar", raw=b"x")[0] == 400


def test_new_server_from_a_modpack_and_local_files(hub_env):
    hub, c = hub_env
    login(c)
    # Local files picked on the setup page wait in the staging area until the server exists.
    status, staged, _ = c.call("POST", "/api/hub/stage?filename=homemade.jar", raw=b"homemade")
    assert status == 200 and len(staged["id"]) == 16
    assert c.call("POST", "/api/hub/stage?filename=notes.txt", raw=b"x")[0] == 400

    pack = make_pack([{"path": "mods/good.jar", "downloads": ["https://cdn.modrinth.com/data/AAA00000/versions/V1111111/good.jar"],
                       "hashes": {"sha1": "0" * 40}, "env": {"server": "required"}}],
                     overrides={"overrides/config/pack.txt": "from the pack"})
    hub.http.json[f"{API}/project/AAA00000"] = hub.http.json[f"{API}/project/AAA"] | {"id": "AAA00000"}
    hub.http.json[f"{API}/project/AAA00000/version?" + "loaders=%5B%22fabric%22%5D"] = \
        hub.http.json[f"{API}/project/AAA/version?" + "loaders=%5B%22fabric%22%5D"]
    url = "https://cdn.modrinth.com/data/PACK0000/versions/PV111111/pack.mrpack"
    hub.http.files[url] = pack
    hub.http.json[f"{API}/version/PV111111"] = {"id": "PV111111", "project_id": "PACK0000", "name": "Pack 1.0",
                                                "files": [{"filename": "pack.mrpack", "url": url, "primary": True,
                                                           "hashes": {"sha1": hashlib.sha1(pack).hexdigest()}}]}
    status, body, _ = c.post("/api/hub/create", {"loader": "fabric", "minecraft": "1.21.1", "motd": "Packed",
                                                  "modpack_version": "PV111111", "local_mods": [staged["id"]],
                                                  "accept_eula": True})
    assert status == 200, body
    d = hub.get(body["id"])
    wait_for(lambda: d.last_job and d.last_job["name"] == "set up server", timeout=30)
    assert d.last_job["ok"], d.last_job
    cfg = d.m.config
    assert cfg.updates.strategy == "mods-only" and "AAA00000" in {m.id for m in cfg.mods}
    assert (d.m.server_dir / "config" / "pack.txt").read_text() == "from the pack"
    assert (d.m.server_dir / "mods" / "homemade.jar").read_bytes() == b"homemade"
    assert "Good Mod" in {m.name for m in lockmod.load(cfg.root).mods}
    assert not (hub.staging_dir / staged["id"]).exists()
    assert c.post("/api/hub/create", {"loader": "fabric", "modpack_version": "nope", "accept_eula": True})[0] == 400
