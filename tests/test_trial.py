"""Try before you buy: which mod broke it, a quick check, and test boots that find culprits."""

from types import SimpleNamespace

from mcsm import trial
from mcsm.diagnose import diagnose
from mcsm.mods.modrinth import ModrinthProvider

from test_hub import login
from test_web import wait_for


def mod(name, filename):
    return SimpleNamespace(name=name, filename=filename)


def test_diagnosis_names_the_mod(tmp_path):
    mods = [mod("Sodium", "sodium-0.6.jar"), mod("Create", "create-6.0.jar"), mod("Lithium", "lithium.jar")]
    fabric = ["Incompatible mods found!", "A potential solution has been determined:",
              "\t - Remove mod 'Sodium' (sodium) 0.6.0 from your mods folder."]
    d = diagnose(fabric, None, mods)
    assert [s.name for s in d.suspects] == ["Sodium"] and "sodium" in d.summary.lower()
    neo = ["Missing or unsupported mandatory dependencies:",
           "\tMod ID: 'flywheel', Requested by: 'create', Expected range: '[1.0,)', Actual version: '[MISSING]'"]
    assert [s.name for s in diagnose(neo, None, mods).suspects] == ["Create"]  # it's Create that can't load
    stack = ["java.lang.NoSuchMethodError: foo", "\tat me.jellysquid.Lithium.x(Lithium.java:3) [lithium.jar:?]",
             "\tat net.minecraft.Server.run(Server.java:1) [server.jar:?]"]
    assert [s.name for s in diagnose(stack, None, mods).suspects] == ["Lithium"]  # server.jar isn't a mod
    # a crash report written during the boot
    sd = tmp_path / "server"
    (sd / "crash-reports").mkdir(parents=True)
    (sd / "crash-reports" / "crash-2026-01-01_00.00.00-server.txt").write_text(
        "---- Minecraft Crash Report ----\nSuspected Mods: Create (create), Lithium (lithium)\n")
    d = diagnose(["something went wrong"], sd, mods, since=0)
    assert {s.name for s in d.suspects} == {"Create", "Lithium"}
    assert diagnose(["Done (1.2s)! For help"], None, mods).suspects == []


def test_quick_check_finds_declared_conflicts(http, modrinth):
    modrinth.project("AAA", "goodmod", "Good Mod")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    modrinth.project("BBB", "grumpy", "Grumpy Mod")
    modrinth.version("BBB", "1.0", ["1.21.1"])
    modrinth.versions["BBB"][-1]["dependencies"].append({"project_id": "AAA", "dependency_type": "incompatible"})
    modrinth._publish("BBB")
    modrinth.project("OLD", "oldmod", "Old Mod")
    modrinth.version("OLD", "1.0", ["1.20.1"])
    r = trial.check(ModrinthProvider(http), ("fabric",), "1.21.1", ["goodmod", "grumpy", "oldmod"])
    assert not r["ok"]
    assert r["conflicts"] == [{"mods": ["Good Mod", "Grumpy Mod"], "reason": "Grumpy Mod says it doesn't work with Good Mod"}]
    assert r["problems"] == [{"mod": "Old Mod", "reason": "no fabric build for Minecraft 1.21.1"}]
    assert trial.check(ModrinthProvider(http), ("fabric",), "1.21.1", ["goodmod"])["ok"]


def test_test_boot_finds_the_culprit(hub_env, modrinth):
    hub, c = hub_env
    login(c)
    modrinth.project("BBB", "othermod", "Other Mod")
    modrinth.version("BBB", "1.0", ["1.21.1"])
    modrinth.project("CRA", "crashmod", "Crash Mod")
    modrinth.version("CRA", "1.0", ["1.21.1"], filename="crash-1.0.jar")
    body = {"loader": "fabric", "minecraft": "1.21.1", "mods": ["goodmod", "crashmod", "othermod"], "bisect": True}
    status, r, _ = c.post("/api/hub/trial", body)
    assert status == 200, r
    assert c.post("/api/hub/trial", body)[0] == 409  # one at a time
    wait_for(lambda: c.get(f"/api/hub/trial?id={r['id']}")[1]["state"] != "running", timeout=60)
    t = c.get(f"/api/hub/trial?id={r['id']}")[1]
    assert t["state"] == "done", t
    res = t["result"]
    assert res["bisected"] and not res["ok"]
    assert sorted(res["working"]) == ["goodmod", "othermod"]
    assert [o["id"] for o in res["outliers"]] == ["crashmod"] and "Crash Mod" in res["outliers"][0]["reason"]
    assert "Crash Mod" in (res["diagnosis"] or {}).get("summary", "")
    assert not (hub.state_dir / "trials" / r["id"]).exists()  # cleaned up

    # Without bisect: just the verdict. And a quick test of an existing server's mods.
    status, r, _ = c.post("/api/hub/trial", {"server": "alpha"})
    wait_for(lambda: c.get(f"/api/hub/trial?id={r['id']}")[1]["state"] != "running", timeout=60)
    assert c.get(f"/api/hub/trial?id={r['id']}")[1]["result"]["ok"]
    assert c.post("/api/servers/alpha/mods/check")[1]["ok"]
    assert c.post("/api/hub/mods/check", {"loader": "fabric", "minecraft": "1.21.1", "mods": ["goodmod", "crashmod"]})[1]["ok"]


def test_backups_made_in_the_same_second_are_both_kept(tmp_path):
    from mcsm import backup
    sd = tmp_path / "server"
    sd.mkdir()
    (sd / "a.txt").write_text("a")
    first = backup.create(sd, tmp_path / "b", "same", [])
    second = backup.create(sd, tmp_path / "b", "same", [])
    assert first != second and first.exists() and second.exists()
    assert backup.list_backups(tmp_path / "b") == [first, second]  # still in order


def test_check_runs_in_the_background_and_survives_a_slow_modrinth(hub_env, modrinth):
    from mcsm.http import HttpError
    from mcsm.mods.modrinth import API
    hub, c = hub_env
    login(c)
    modrinth.project("AAA", "goodmod", "Good Mod")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    modrinth.project("SLO", "slowmod", "Slow Mod")
    hub.http.json[f"{API}/project/SLO/version"] = HttpError(
        f"{API}/project/SLO/version", None, "request failed: The read operation timed out")
    body = {"loader": "fabric", "minecraft": "1.21.1", "mods": ["goodmod", "slowmod"]}
    # answered straight away (as before), with the slow mod reported rather than failing it all
    r = c.post("/api/hub/mods/check", body)[1]
    assert [m["name"] for m in r["mods"]] == ["Good Mod"]
    assert r["problems"] == [{"mod": "slowmod", "reason": "couldn't check it: api.modrinth.com took too long to "
                                                          "answer; check your internet connection and try again"}]
    # in the background, with progress
    status, started, _ = c.post("/api/hub/mods/check", {**body, "background": True})
    assert status == 200 and started["id"]
    wait_for(lambda: c.get(f"/api/hub/mods/check?id={started['id']}")[1]["state"] != "running")
    job = c.get(f"/api/hub/mods/check?id={started['id']}")[1]
    assert job["state"] == "done" and job["done"] == job["total"] == 2
    assert job["result"]["problems"] == r["problems"]
    assert c.get("/api/servers/alpha/mods/check")[0] == 404  # (POST only)
    assert c.get("/api/hub/mods/check?id=nope")[0] == 404


def test_a_failed_download_is_explained_not_an_internal_error(hub_env):
    from mcsm.http import HttpError
    from mcsm.mods.modrinth import API
    hub, c = hub_env
    login(c)
    url = f"{API}/project/x1/version"
    hub.http.json[f"{API}/project/x1"] = {"id": "x1", "slug": "x1", "title": "X"}
    hub.http.json[url] = HttpError(url, None, "request failed: The read operation timed out")
    status, body, _ = c.get("/api/hub/mods/requires?id=x1&loader=fabric&version=1.21.1")
    assert status == 502 and body["error"].startswith("api.modrinth.com took too long")
