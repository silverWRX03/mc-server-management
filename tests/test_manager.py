"""End-to-end upgrade tests against a fake `java` and a fake Minecraft server."""

from mcsm import backup, lock as lockmod
from mcsm.config import ModSpec
from mcsm.daemon import Daemon
from mcsm.manager import Manager
from mcsm.mods import providers_for

from conftest import FakeLoader, FakeMojang


def manager(cfg, http, releases):
    mojang = FakeMojang(http, releases)
    loader = FakeLoader(http, mojang)
    return Manager(cfg, http=http, mojang=mojang, loader=loader, providers=providers_for(cfg, http),
                   echo=False, sleep=lambda s: None)


def update(m):
    decision, changes = m.check()
    assert decision.plan is not None
    return m.apply(decision.plan)


def test_install_upgrade_and_rollback(make_config, http, modrinth):
    modrinth.project("AAA", "goodmod")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "goodmod")])
    m = manager(cfg, http, ["1.21.1"])
    server = cfg.server.dir

    # Fresh install on 1.21.1, including a verification boot.
    result = update(m)
    assert result.ok, result.message
    assert (server / "mods" / "AAA-1.0.jar").exists()
    assert (server / "runtime-1.21.1.txt").exists()
    assert lockmod.load(cfg.root).minecraft == "1.21.1"

    # A hand-added jar is never touched.
    (server / "mods" / "handmade.jar").write_bytes(b"mine")
    (server / "world").mkdir()
    (server / "world" / "level.dat").write_bytes(b"level")

    # 1.21.2 comes out and the mod updates for it.
    m.mojang.set_releases(["1.21.1", "1.21.2"])
    modrinth.version("AAA", "1.1", ["1.21.2"])
    result = update(m)
    assert result.ok, result.message
    assert not (server / "mods" / "AAA-1.0.jar").exists()
    assert (server / "mods" / "AAA-1.1.jar").exists()
    assert (server / "mods" / "handmade.jar").exists()
    assert not (server / "runtime-1.21.1.txt").exists()  # old runtime removed
    assert (server / "runtime-1.21.2.txt").exists()
    assert (server / "world" / "level.dat").read_bytes() == b"level"
    assert result.backup and result.backup.exists()
    assert m.lock.minecraft == "1.21.2"

    # 1.21.3: the new mod build crashes on boot -> automatic rollback.
    m.mojang.set_releases(["1.21.1", "1.21.2", "1.21.3"])
    modrinth.version("AAA", "1.2", ["1.21.3"], filename="crash-AAA-1.2.jar")
    result = update(m)
    assert not result.ok
    assert "rolled back" in result.message
    assert (server / "mods" / "AAA-1.1.jar").exists()
    assert not (server / "mods" / "crash-AAA-1.2.jar").exists()
    assert (server / "runtime-1.21.2.txt").exists()
    saved = lockmod.load(cfg.root)
    assert saved.minecraft == "1.21.2"
    assert len(saved.failed_plans) == 1

    # The failed combination is not retried; nothing else is newer, so stay put.
    decision, changes = m.check()
    assert decision.plan.minecraft == "1.21.2"
    assert changes.empty
    assert m.check(retry_failed=True)[0].plan.minecraft == "1.21.3"  # but a manual update retries


def test_download_failure_leaves_server_untouched(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "AAA")])
    m = manager(cfg, http, ["1.21.1"])
    assert update(m).ok
    before = backup.list_backups(cfg.backups.dir)

    m.mojang.set_releases(["1.21.1", "1.21.2"])
    modrinth.version("AAA", "1.1", ["1.21.2"])
    url = next(u for u in http.files if "AAA-1.1" in u)
    http.files[url] = b"corrupted"  # sha1 no longer matches
    result = update(m)
    assert not result.ok
    assert "could not prepare" in result.message
    assert (cfg.server.dir / "mods" / "AAA-1.0.jar").exists()
    assert m.lock.minecraft == "1.21.1"
    assert backup.list_backups(cfg.backups.dir) == before  # never got as far as stopping


def test_unchanged_mods_are_not_redownloaded(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    modrinth.project("BBB")
    modrinth.version("BBB", "1.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "AAA"), ModSpec("modrinth", "BBB")])
    m = manager(cfg, http, ["1.21.1"])
    assert update(m).ok
    http.downloads.clear()
    modrinth.version("BBB", "1.1", ["1.21.1"])
    result = update(m)
    assert result.ok
    assert len(http.downloads) == 1 and "BBB-1.1" in http.downloads[0]
    assert m.loader.installs == ["1.21.1"]  # runtime not reinstalled for a mod-only update


def test_daemon_applies_update_to_running_server(make_config, http, modrinth):
    modrinth.project("AAA")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "AAA")])
    m = manager(cfg, http, ["1.21.1"])
    assert update(m).ok

    d = Daemon(m)
    d.proc = m.start_server()
    try:
        assert d.proc.players_online() == 0
        m.mojang.set_releases(["1.21.1", "1.21.2"])
        modrinth.version("AAA", "1.1", ["1.21.2"])
        old = d.proc
        d.check_for_updates()
        assert m.lock.minecraft == "1.21.2"
        assert not old.running
        assert d.proc is not old and d.proc.running
    finally:
        d.proc.stop(10)


def test_import_existing_mods(make_config, http, modrinth):
    import hashlib

    from mcsm.mods.modrinth import API

    modrinth.project("AAA", "goodmod", "Good Mod")
    cfg = make_config([])
    mods = cfg.server.dir / "mods"
    mods.mkdir()
    (mods / "good.jar").write_bytes(b"good")
    (mods / "mystery.jar").write_bytes(b"???")
    sha = hashlib.sha1(b"good").hexdigest()
    http.posts[f"{API}/version_files"] = {sha: {"id": "v1", "project_id": "AAA", "version_number": "3.0"}}

    m = manager(cfg, http, ["1.21.1"])
    identified, unknown = m.import_existing()
    assert [x.name for x in identified] == ["Good Mod"]
    assert unknown == ["mystery.jar"]
    assert m.lock.minecraft == "1.21.1"
    assert m.unmanaged_jars() == ["mystery.jar"]
