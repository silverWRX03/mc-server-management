"""Picking a mod brings the mods it needs; they're shown and removed together."""

from mcsm.mods.modrinth import ModrinthProvider
from mcsm.web import mod_requirements

from test_hub import login
from test_manager import update


def publish(modrinth):
    modrinth.project("LIB", "deplib", "Dep Lib")
    modrinth.version("LIB", "1.0", ["1.21.1"])
    modrinth.project("MAP", "minimap", "Mini Map", server_side="unsupported")
    modrinth.version("MAP", "1.0", ["1.21.1"])
    modrinth.project("MID", "midlib", "Mid Lib")
    modrinth.version("MID", "1.0", ["1.21.1"], deps=["LIB"])
    modrinth.project("TOP", "topmod", "Top Mod")
    modrinth.version("TOP", "1.0", ["1.21.1"], deps=["MID", "MAP"])
    modrinth.project("OLD", "oldmod", "Old Mod")
    modrinth.version("OLD", "1.0", ["1.20.1"])


def test_requirements(http, modrinth):
    publish(modrinth)
    p = ModrinthProvider(http)
    r = mod_requirements(p, "topmod", ("fabric",), "1.21.1")
    assert r["compatible"] and r["project"]["name"] == "Top Mod"
    # dependencies of dependencies too; client-only ones aren't server mods
    assert [(d["name"], d["needed_by"]) for d in r["deps"]] == [("Mid Lib", "Top Mod"), ("Dep Lib", "Mid Lib")]
    old = mod_requirements(p, "oldmod", ("fabric",), "1.21.1")
    assert not old["compatible"] and "no build for Minecraft 1.21.1" in old["reason"]
    assert mod_requirements(p, "oldmod", ("fabric",), None)["compatible"]  # any version


def test_mods_page_groups_and_checks(hub_env, modrinth):
    hub, c = hub_env
    login(c)
    publish(modrinth)
    r = c.get("/api/hub/mods/requires?id=topmod&loader=fabric&version=1.21.1")[1]
    assert [d["slug"] for d in r["deps"]] == ["midlib", "deplib"]
    assert c.get("/api/hub/mods/requires?id=topmod&loader=vanilla")[0] == 400

    status, body, _ = c.post("/api/servers/alpha/mods/add", {"source": "modrinth", "id": "topmod"})
    assert status == 200 and body["deps"] == ["Mid Lib", "Dep Lib"]
    status, body, _ = c.post("/api/servers/alpha/mods/add", {"source": "modrinth", "id": "oldmod"})
    assert status == 400 and "no build for Minecraft 1.21.1" in body["error"]  # not for this server's version

    alpha = hub.get("alpha")
    assert update(alpha.m).ok
    configured = {m["id"]: m for m in c.get("/api/servers/alpha/mods")[1]["configured"]}
    assert configured["topmod"]["name"] == "Top Mod"
    assert [d["name"] for d in configured["topmod"]["deps"]] == ["Mid Lib", "Dep Lib"]
    # Removing the mod takes its dependencies with it at the next update.
    assert c.post("/api/servers/alpha/mods/remove", {"source": "modrinth", "id": "topmod"})[0] == 200
    assert update(alpha.m).ok
    assert not {"Top Mod", "Mid Lib", "Dep Lib"} & {m.name for m in alpha.m.lock.mods}


def test_mod_lists_only_show_mods_for_the_chosen_version(hub_env):
    import json
    from mcsm.mods.modrinth import API
    hub, c = hub_env
    login(c)
    hub.http.json[f"{API}/search"] = {"hits": []}
    seen, orig = [], hub.http.get_json
    hub.http.get_json = lambda url, params=None, headers=None: seen.append(params) or orig(url, params, headers)
    facets = lambda: json.loads([p for p in seen if p and "facets" in p][-1]["facets"])  # noqa: E731
    assert c.get("/api/hub/mods/search?loader=fabric&version=1.20.1&top=1")[0] == 200
    assert ["versions:1.20.1"] in facets()
    assert c.get("/api/servers/alpha/mods/search?q=x")[0] == 200
    assert ["versions:1.21.1"] in facets()  # the server's own version
    assert c.get("/api/servers/alpha/client/search?top=1")[0] == 200
    assert ["versions:1.21.1"] in facets()
    assert c.get("/api/hub/mods/search?loader=fabric&version=$(x)&top=1")[0] == 400
    hub.http.get_json = orig


def test_shared_dependency_is_listed_under_each_mod_and_stays(hub_env, modrinth):
    hub, c = hub_env
    login(c)
    publish(modrinth)
    modrinth.project("SIB", "sibmod", "Sib Mod")
    modrinth.version("SIB", "1.0", ["1.21.1"], deps=["LIB"])
    for mod in ("topmod", "sibmod"):
        assert c.post("/api/servers/alpha/mods/add", {"source": "modrinth", "id": mod})[0] == 200
    alpha = hub.get("alpha")
    assert update(alpha.m).ok
    configured = {m["id"]: m for m in c.get("/api/servers/alpha/mods")[1]["configured"]}
    assert "Dep Lib" in [d["name"] for d in configured["topmod"]["deps"]]
    assert [d["name"] for d in configured["sibmod"]["deps"]] == ["Dep Lib"]
    # Removing one of them keeps what the other still needs.
    assert c.post("/api/servers/alpha/mods/remove", {"source": "modrinth", "id": "topmod"})[0] == 200
    assert update(alpha.m).ok
    names = {m.name for m in alpha.m.lock.mods}
    assert {"Sib Mod", "Dep Lib"} <= names and not {"Top Mod", "Mid Lib"} & names
