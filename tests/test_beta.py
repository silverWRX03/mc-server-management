"""Trying beta Minecraft versions: offered in setup, and tested on a copy of a server."""

from mcsm import setup as setupmod
from mcsm.config import ModSpec
from mcsm.lock import Lock

from conftest import FakeMojang
from test_hub import login
from test_planner import planner
from test_web import wait_for


def test_betas_are_listed_newest_first(http):
    mojang = FakeMojang(http, ["1.21.1", "1.21.2"])
    assert mojang.betas() == ["99w01a"]
    for v in ("99w01a", "1.21.5-pre1", "1.21.5-rc2"):
        assert setupmod.SetupSpec.from_dict({"loader": "fabric", "minecraft": v, "accept_eula": True}).minecraft == v


def test_a_beta_server_stays_on_its_beta(make_config, http, modrinth):
    modrinth.project("AAA", "lithium")
    modrinth.version("AAA", "1.0", ["1.21.1", "99w01a"])
    cfg = make_config([ModSpec("modrinth", "lithium")], strategy="latest-compatible")
    decision = planner(cfg, http, lock=Lock(minecraft="99w01a", launch=["x"])).decide()
    assert decision.plan.minecraft == "99w01a"  # never "upgraded" back to an older release


def test_test_copy_on_a_beta(hub_env):
    hub, c = hub_env
    login(c)
    r = c.get("/api/servers/alpha/beta")[1]
    assert r["betas"] == ["99w01a"] and r["copies"]
    assert c.post("/api/servers/alpha/beta/test", {"version": "1.21.1"})[0] == 400
    assert c.post("/api/servers/alpha/beta/test", {"version": "99w01a"})[0] == 200
    alpha = hub.get("alpha")
    wait_for(lambda: alpha.last_job and alpha.last_job["name"] == "beta test copy", timeout=30)
    assert alpha.last_job["ok"], alpha.last_job
    copy = hub.get("alpha-beta-99w01a")
    wait_for(lambda: copy.last_job and copy.last_job["name"] == "install beta", timeout=30)
    assert copy.last_job["ok"], copy.last_job
    assert copy.m.lock.minecraft == "99w01a" and copy.m.config.updates.strategy == "mods-only"
    assert all(not s.required for s in copy.m.config.mods)
    assert alpha.m.lock.minecraft == "1.21.1"  # the original is untouched
    assert c.get("/api/hub")[1]["servers"]
