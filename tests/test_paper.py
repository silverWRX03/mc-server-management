"""Paper servers: builds from PaperMC, plugins in plugins/, players join with plain Minecraft."""

import hashlib
import io
import json
import urllib.parse
import zipfile

import pytest

from mcsm import configs, diagnose, lock as lockmod
from mcsm.browse import Browser, BrowseError
from mcsm.clientpack import PackBuilder
from mcsm.config import ModSpec
from mcsm.http import HttpError
from mcsm.loaders import LOADERS, PaperLoader, mods_folder
from mcsm.loaders.paper import FILL, PAPER_V2
from mcsm.manager import Manager
from mcsm.mods import providers_for
from mcsm.mods.modrinth import API

from conftest import FakeLoader, FakeMojang
from test_manager import update

PAPER = b"paper server jar"


def fill_builds(http, minecraft="1.21.4"):
    sha = hashlib.sha256(PAPER).hexdigest()
    http.json[f"{FILL}/versions/{minecraft}/builds"] = [
        {"id": 12, "channel": "BETA", "downloads": {"server:default": {"name": "p-12.jar", "url": "https://fill.test/12.jar"}}},
        {"id": 10, "channel": "STABLE", "downloads": {"server:default": {
            "name": "p-10.jar", "url": "https://fill.test/10.jar", "checksums": {"sha256": sha}}}},
        {"id": 11, "channel": "STABLE", "downloads": {"server:default": {
            "name": "p-11.jar", "url": "https://fill.test/11.jar", "checksums": {"sha256": sha}}}},
    ]
    http.files["https://fill.test/11.jar"] = PAPER


def test_paper_is_a_loader_with_a_plugins_folder():
    assert "paper" in LOADERS and mods_folder("paper") == "plugins" and mods_folder("fabric") == "mods"


def test_newest_stable_build_is_installed(http, tmp_path):
    fill_builds(http)
    loader = PaperLoader(http, FakeMojang(http, ["1.21.4"]))
    assert loader.latest_version("1.21.4") == "11"  # the beta build 12 isn't used
    assert loader.latest_version("1.99") is None  # Paper doesn't have it (yet)
    rt = loader.install("1.21.4", "11", tmp_path, "java")
    assert (tmp_path / "paper.jar").read_bytes() == PAPER
    assert rt.files == ["paper.jar"] and rt.launch == ["-jar", "paper.jar", "--nogui"]


def test_older_api_when_fill_is_down(http, tmp_path):
    http.json[f"{FILL}/versions/1.21.1/builds"] = HttpError("x", 503, "unavailable")
    http.json[f"{PAPER_V2}/versions/1.21.1/builds"] = {"builds": [
        {"build": 130, "channel": "default", "downloads": {"application": {
            "name": "paper-1.21.1-130.jar", "sha256": hashlib.sha256(PAPER).hexdigest()}}},
        {"build": 131, "channel": "experimental", "downloads": {"application": {"name": "paper-1.21.1-131.jar"}}}]}
    http.files[f"{PAPER_V2}/versions/1.21.1/builds/130/downloads/paper-1.21.1-130.jar"] = PAPER
    loader = PaperLoader(http, FakeMojang(http, ["1.21.1"]))
    assert loader.latest_version("1.21.1") == "130"
    loader.install("1.21.1", "130", tmp_path, "java")
    assert (tmp_path / "paper.jar").read_bytes() == PAPER


class FakePaper(FakeLoader):
    name = "paper"
    mod_loaders = PaperLoader.mod_loaders
    mods_folder = "plugins"


def test_plugins_install_into_the_plugins_folder_and_players_need_nothing(make_config, http, modrinth):
    modrinth.project("LP", "luckperms", "LuckPerms", client_side="unsupported")
    modrinth.version("LP", "5.4", ["1.21.1"], loaders=("paper", "bukkit"))
    key = f"{API}/project/LP/version?" + urllib.parse.urlencode({"loaders": json.dumps(list(PaperLoader.mod_loaders))})
    http.json[key] = modrinth.versions["LP"]
    cfg = make_config([ModSpec("modrinth", "luckperms")])
    mojang = FakeMojang(http, ["1.21.1"])
    m = Manager(cfg, http=http, mojang=mojang, loader=FakePaper(http, mojang), providers=providers_for(cfg, http),
                echo=False, sleep=lambda s: None)
    assert update(m).ok
    assert (cfg.server.dir / "plugins" / "LP-5.4.jar").is_file()
    assert not (cfg.server.dir / "mods").exists()
    assert [x.name for x in lockmod.load(cfg.root).mods] == ["LuckPerms"]
    pack = PackBuilder(m).build("example.org:25565")
    assert pack["loader"] == "vanilla" and pack["loader_version"] is None and pack["mods"] == []


def test_plugin_search_on_modrinth_only(http):
    seen = []
    http.json[f"{API}/search"] = {"total_hits": 0, "hits": []}
    orig = http.get_json
    http.get_json = lambda url, params=None, headers=None: seen.append(params) or orig(url, params, headers)
    b = Browser(http, "key")
    b.search("modrinth", "mod", "perms", loader="paper", version="1.21.1")
    facets = json.loads([p for p in seen if p and "facets" in p][-1]["facets"])
    assert ["categories:paper", "categories:spigot", "categories:bukkit"] in facets
    assert ["project_type:mod"] not in facets
    with pytest.raises(BrowseError, match="Modrinth"):
        b.search("curseforge", "mod", "perms", loader="paper")


def plugin_jar(path, name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("plugin.yml", f"name: {name}\nversion: 1.0\nmain: x.Main\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


def test_plugin_configs_and_blame(tmp_path):
    server = tmp_path / "server"
    plugin_jar(server / "plugins" / "Chunky-1.4.jar", "Chunky")
    (server / "plugins" / "Chunky").mkdir()
    (server / "plugins" / "Chunky" / "config.yml").write_text("language: en\n")
    (server / "config").mkdir()
    (server / "config" / "paper-global.yml").write_text("a: 1\n")
    g = configs.grouped(server, [])
    assert g["mods"] == [{"mod_id": "Chunky", "name": "Chunky", "jar": "Chunky-1.4.jar",
                          "files": ["plugins/Chunky/config.yml"]}]
    assert g["other"] == ["config/paper-global.yml"]
    assert configs.read(server, "plugins/Chunky/config.yml")["format"] == "yaml"
    d = diagnose.diagnose(["[12:00:00 ERROR]: Error occurred while enabling Chunky v1.4 (Is it up to date?)"], server)
    assert [s.filename for s in d.suspects] == ["Chunky-1.4.jar"]
