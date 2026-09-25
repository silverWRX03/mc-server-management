from mcsm.config import ModSpec
from mcsm.lock import Lock
from mcsm.mods import providers_for
from mcsm.planner import Planner

from conftest import FakeLoader, FakeMojang

RELEASES = ["1.21.1", "1.21.2", "1.21.3", "1.21.4"]


def planner(config, http, lock=None, supported=None):
    mojang = FakeMojang(http, RELEASES)
    loader = FakeLoader(http, mojang, supported)
    return Planner(config, lock or Lock(minecraft="1.21.1", launch=["x"]), mojang, loader,
                   providers_for(config, http))


def test_picks_newest_version_all_required_mods_support(make_config, http, modrinth):
    modrinth.project("AAA", "lithium")
    modrinth.version("AAA", "1.0", RELEASES)
    modrinth.project("BBB", "create")
    modrinth.version("BBB", "2.0", ["1.21.1", "1.21.2", "1.21.3"])
    cfg = make_config([ModSpec("modrinth", "lithium"), ModSpec("modrinth", "create")])

    decision = planner(cfg, http).decide()

    assert decision.plan.minecraft == "1.21.3"
    assert decision.latest == "1.21.4"
    assert [p.minecraft for p in decision.blocked] == ["1.21.4"]
    assert [b.name for b in decision.blocked[0].blockers] == ["BBB"]


def test_optional_mod_is_dropped_instead_of_blocking(make_config, http, modrinth):
    modrinth.project("AAA", "lithium")
    modrinth.version("AAA", "1.0", RELEASES)
    modrinth.project("BBB", "create")
    modrinth.version("BBB", "2.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "lithium"), ModSpec("modrinth", "create", required=False)])

    plan = planner(cfg, http).decide().plan

    assert plan.minecraft == "1.21.4"
    assert [m.name for m in plan.mods] == ["AAA"]
    assert [b.name for b in plan.dropped] == ["BBB"]


def test_latest_strategy_waits_for_newest_release(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", ["1.21.1", "1.21.2", "1.21.3"])
    modrinth.version("AAA", "1.1", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "AAA")], strategy="latest")

    decision = planner(cfg, http).decide()

    # Doesn't jump to 1.21.3; stays on 1.21.1 and just updates mods.
    assert decision.plan.minecraft == "1.21.1"
    assert decision.plan.mods[0].version_number == "1.1"
    assert [p.minecraft for p in decision.blocked] == ["1.21.4"]


def test_mods_only_never_changes_minecraft(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", RELEASES)
    cfg = make_config([ModSpec("modrinth", "AAA")], strategy="mods-only")
    assert planner(cfg, http).decide().plan.minecraft == "1.21.1"


def test_loader_support_blocks_versions(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", RELEASES)
    cfg = make_config([ModSpec("modrinth", "AAA")])
    decision = planner(cfg, http, supported={"1.21.1", "1.21.2"}).decide()
    assert decision.plan.minecraft == "1.21.2"
    assert all(p.loader_version is None for p in decision.blocked)


def test_dependencies_are_resolved_and_can_block(make_config, http, modrinth):
    modrinth.project("AAA", "mymod")
    modrinth.version("AAA", "1.0", RELEASES, deps=["LIB"])
    modrinth.project("LIB", "somelib")
    modrinth.version("LIB", "0.5", ["1.21.1", "1.21.2"])
    cfg = make_config([ModSpec("modrinth", "mymod")])

    decision = planner(cfg, http).decide()

    assert decision.plan.minecraft == "1.21.2"
    names = {m.name: m for m in decision.plan.mods}
    assert names["LIB"].dependency_of == "modrinth:AAA"
    blocker = decision.blocked[0].blockers
    assert {b.name for b in blocker} == {"LIB", "AAA"}


def test_client_only_dependency_is_skipped(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", RELEASES, deps=["CLI"])
    modrinth.project("CLI", server_side="unsupported")
    cfg = make_config([ModSpec("modrinth", "AAA")])
    plan = planner(cfg, http).decide().plan
    assert plan.minecraft == "1.21.4"
    assert [m.name for m in plan.mods] == ["AAA"]


def test_prerelease_mods_respect_channel(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    modrinth.version("AAA", "2.0-beta", RELEASES, version_type="beta")

    assert planner(make_config([ModSpec("modrinth", "AAA")]), http).decide().plan.minecraft == "1.21.1"
    beta = make_config([ModSpec("modrinth", "AAA")], mod_channel="beta")
    assert planner(beta, http).decide().plan.minecraft == "1.21.4"


def test_failed_plan_is_not_retried(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", RELEASES)
    cfg = make_config([ModSpec("modrinth", "AAA")])
    p = planner(cfg, http)
    first = p.decide().plan
    p.lock.failed_plans[first.fingerprint] = "crashed"
    decision = p.decide()
    assert decision.plan.minecraft == "1.21.3"
    assert "update manually" in decision.blocked[0].blockers[0].reason
    # Asking for it (a manual update) tries it again.
    assert p.decide(retry_failed=True).plan.minecraft == first.minecraft
    # With nothing installed yet, a failure never blocks the first install.
    p.lock = Lock()
    fresh = p.decide().plan
    p.lock.failed_plans[fresh.fingerprint] = "crashed"
    assert p.decide().plan.fingerprint == fresh.fingerprint


def test_changes_diff(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", RELEASES)
    cfg = make_config([ModSpec("modrinth", "AAA")], strategy="mods-only")
    p = planner(cfg, http)
    plan = p.decide().plan
    lock = Lock(minecraft="1.21.1", loader="fabric", loader_version="loader-1.21.1", launch=["x"],
                mods=[plan.mods[0]])
    assert plan.changes(lock).empty
    modrinth.version("AAA", "1.1", RELEASES)
    http_plan = planner(cfg, http, lock).decide().plan
    changes = http_plan.changes(lock)
    assert not changes.runtime
    assert [(o.version_number, n.version_number) for o, n in changes.updated] == [("1.0", "1.1")]
