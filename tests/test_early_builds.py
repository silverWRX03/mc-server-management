"""Mod lists only offer mods that really run on the chosen version; alpha/beta-only mods on request."""

import json

import pytest

from mcsm import config as configmod, lock as lockmod, setup as setupmod
from mcsm.browse import Browser
from mcsm.config import ConfigError, ModSpec
from mcsm.mods.modrinth import API, ModrinthProvider

from test_hub import login
from test_manager import manager, update


def publish(modrinth, http):
    modrinth.project("GOOD", "goodmod", "Good Mod")
    modrinth.version("GOOD", "1.0", ["1.21.1"])
    modrinth.project("FORGE", "forgeonly", "Forge Only")  # 1.21.1 only on Forge, Fabric only for 1.20.1
    modrinth.version("FORGE", "1.0", ["1.21.1"], loaders=("forge",))
    modrinth.version("FORGE", "0.9", ["1.20.1"])
    modrinth.project("BETA", "betamod", "Beta Mod")
    modrinth.version("BETA", "0.1", ["1.21.1"], version_type="beta")
    hits = [{"project_id": pid, "slug": slug, "title": pid, "description": "", "downloads": 1, "versions": ["1.21.1"]}
            for pid, slug in (("GOOD", "goodmod"), ("FORGE", "forgeonly"), ("BETA", "betamod"))]
    http.json[f"{API}/search"] = {"total_hits": 3, "hits": hits}


def test_only_mods_with_a_real_build_are_listed(http, modrinth):
    publish(modrinth, http)
    b = Browser(http)
    r = b.search("modrinth", "mod", "", loader="fabric", version="1.21.1")
    assert [x["slug"] for x in r["results"]] == ["goodmod"] and r["results"][0]["channel"] == "release"
    assert r["hidden"] == 1 and r["early_hidden"] == 1
    r = b.search("modrinth", "mod", "", loader="fabric", version="1.21.1", early=True)
    assert [(x["slug"], x["channel"]) for x in r["results"]] == [("goodmod", "release"), ("betamod", "beta")]
    assert ModrinthProvider(http).best_channels(["GOOD", "FORGE", "BETA"], ("fabric",), "1.21.1") == \
        {"GOOD": "release", "FORGE": None, "BETA": "beta"}


def test_setup_and_mods_page_lists(hub_env, modrinth):
    hub, c = hub_env
    login(c)
    publish(modrinth, hub.http)
    r = c.get("/api/hub/mods/search?loader=fabric&version=1.21.1&q=mod")[1]
    assert [x["slug"] for x in r["results"]] == ["goodmod"] and (r["hidden"], r["early_hidden"]) == (1, 1)
    r = c.get("/api/hub/mods/search?loader=fabric&version=1.21.1&q=mod&early=1")[1]
    assert [x["slug"] for x in r["results"]] == ["goodmod", "betamod"]
    r = c.get("/api/servers/alpha/mods/search?q=mod")[1]  # the server's own version
    assert [x["slug"] for x in r["results"]] == ["goodmod"]
    # A beta-only mod: only with its own channel, which is kept in mcsm.toml.
    assert c.get("/api/hub/mods/requires?id=betamod&loader=fabric&version=1.21.1")[1]["compatible"] is False
    assert c.get("/api/hub/mods/requires?id=betamod&loader=fabric&version=1.21.1&channel=beta")[1]["compatible"]
    assert c.post("/api/servers/alpha/mods/add", {"source": "modrinth", "id": "betamod"})[0] == 400
    status, body, _ = c.post("/api/servers/alpha/mods/add-many",
                             {"mods": [{"source": "modrinth", "id": "betamod", "channel": "beta"}]})
    assert status == 200 and body["added"] == ["Beta Mod"]
    alpha = hub.get("alpha")
    assert [s.channel for s in alpha.m.config.mods if s.id == "betamod"] == ["beta"]
    assert 'channel = "beta"' in (alpha.m.config.path).read_text()
    configured = {m["id"]: m for m in c.get("/api/servers/alpha/mods")[1]["configured"]}
    assert configured["betamod"]["channel"] == "beta"
    # the quick check takes the mod's own channel into account
    assert c.post("/api/servers/alpha/mods/check", {})[1]["problems"] == []
    r = c.post("/api/hub/mods/check", {"loader": "fabric", "minecraft": "1.21.1", "mods": ["betamod"]})[1]
    assert r["problems"] and "no fabric build" in r["problems"][0]["reason"]
    r = c.post("/api/hub/mods/check", {"loader": "fabric", "minecraft": "1.21.1", "mods": ["betamod"],
                                       "channels": {"betamod": "beta"}})[1]
    assert r["ok"]


def test_a_mods_own_channel_is_used_for_updates(make_config, http, modrinth):
    modrinth.project("BETA", "betamod", "Beta Mod")
    modrinth.version("BETA", "0.1", ["1.21.1"], version_type="beta")
    modrinth.project("GOOD", "goodmod", "Good Mod")
    modrinth.version("GOOD", "1.0", ["1.21.1"])
    modrinth.version("GOOD", "1.1-beta", ["1.21.1"], version_type="beta")
    cfg = make_config([ModSpec("modrinth", "betamod", channel="beta"), ModSpec("modrinth", "goodmod")])
    assert [s.channel for s in cfg.mods] == ["beta", None]
    m = manager(cfg, http, ["1.21.1"])
    assert update(m).ok
    installed = {x.name: x.version_number for x in lockmod.load(cfg.root).mods}
    assert installed == {"Beta Mod": "0.1", "Good Mod": "1.0"}  # the others stay on releases


def test_config_and_setup_accept_a_channel(tmp_path, make_config):
    cfg = make_config()
    configmod.append_mod(cfg.path, ModSpec("modrinth", "x"))
    cfg.path.write_text(cfg.path.read_text() + '\n[[mods]]\nid = "y"\nchannel = "nightly"\n')
    with pytest.raises(ConfigError, match="channel"):
        configmod.load(cfg.root)
    spec = setupmod.SetupSpec.from_dict({"loader": "fabric", "mods": ["a", "b"], "accept_eula": True,
                                         "mod_channels": {"a": "alpha", "b": "release", "c": "x"}})
    assert spec.mod_channels == {"a": "alpha"}
    assert json.dumps(spec.mod_channels)
