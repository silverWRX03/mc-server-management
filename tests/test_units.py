
import pytest

from mcsm import backup, config as configmod
from mcsm.config import ConfigError, ModSpec, parse_duration
from mcsm.java import JavaError
from mcsm.java import JavaManager, parse_major
from mcsm.loaders.base import version_key
from mcsm.loaders.forge import NEOFORGE_VERSIONS, NeoForgeLoader, neoforge_prefix
from mcsm.loaders.fabric import FABRIC_META, FabricLoader
from mcsm.process import PLAYERS, READY
from mcsm.rcon import decode, encode

from conftest import FakeMojang


def test_durations():
    assert parse_duration("6h") == 21600
    assert parse_duration("30m") == 1800
    assert parse_duration(90) == 90
    with pytest.raises(ConfigError):
        parse_duration("soon")


def test_template_loads_and_mod_blocks_round_trip(tmp_path):
    (tmp_path / "mcsm.toml").write_text(configmod.render_template("neoforge", "1.21.1"))
    path = tmp_path / "mcsm.toml"
    configmod.append_mod(path, ModSpec("modrinth", "create"))
    configmod.append_mod(path, ModSpec("curseforge", "238222", required=False))
    cfg = configmod.load(tmp_path)
    assert cfg.server.loader == "neoforge"
    assert [(m.source, m.id, m.required) for m in cfg.mods] == [
        ("modrinth", "create", True), ("curseforge", "238222", False)]

    assert configmod.remove_mod(path, "modrinth", "create")
    assert not configmod.remove_mod(path, "modrinth", "create")
    cfg = configmod.load(tmp_path)
    assert [m.id for m in cfg.mods] == ["238222"]
    assert "# mcsm configuration" in path.read_text()  # comments survive edits


def test_invalid_config(tmp_path):
    (tmp_path / "mcsm.toml").write_text('[server]\nloader = "bukkit"\n')
    with pytest.raises(ConfigError, match="server.loader"):
        configmod.load(tmp_path)


def test_java_version_parsing():
    assert parse_major('openjdk version "21.0.4" 2024-07-16') == 21
    assert parse_major('java version "1.8.0_392"') == 8
    assert parse_major('openjdk version "17" 2021-09-14') == 17


def test_java_selection(tmp_path):
    (tmp_path / "mcsm.toml").write_text(configmod.render_template("fabric", "1.21.1"))
    cfg = configmod.load(tmp_path)
    cfg.java_versions = {17: "/j17", 21: "/j21"}
    cfg.java_auto_install = False
    majors = {"/j17": 17, "/j21": 21, "java": 25}
    jm = JavaManager(cfg, probe_fn=majors.get)
    assert jm.select(21) == "/j21"
    assert jm.select(17) == "/j17"
    assert jm.select(25) == "java"
    assert jm.select(16) == "/j17"  # no exact match: lowest newer one
    cfg.java_version = 21
    assert jm.select(17) == "/j21"  # forced
    with pytest.raises(JavaError, match="needs Java 25"):
        jm.select(25)


def test_config_set_value_keeps_comments(tmp_path):
    path = tmp_path / "mcsm.toml"
    path.write_text(configmod.render_template("fabric", "1.21.1"))
    configmod.set_value(path, "java", "version", "21")
    configmod.set_value(path, "server", "memory", '"8G"')
    configmod.set_value(path, "brand_new", "key", "true")
    cfg = configmod.load(tmp_path)
    assert cfg.java_version == 21
    assert cfg.server.memory == "8G"
    assert "# download Eclipse Temurin" in path.read_text()


def test_server_properties_editing(tmp_path):
    from mcsm.properties import read_properties, write_properties
    path = tmp_path / "server.properties"
    path.write_text("#Minecraft server properties\nmotd=old\nview-distance=10\n")
    write_properties(path, {"motd": "new", "server-port": "25570"})
    assert read_properties(path) == {"motd": "new", "view-distance": "10", "server-port": "25570"}
    assert path.read_text().startswith("#Minecraft")


def test_neoforge_versions(http):
    assert neoforge_prefix("1.21.1") == "21.1."
    assert neoforge_prefix("1.21") == "21.0."
    assert neoforge_prefix("26.1") == "26.1."
    http.json[NEOFORGE_VERSIONS] = {"versions": ["21.1.9", "21.1.77", "21.1.100-beta", "21.2.1", "21.1.10"]}
    loader = NeoForgeLoader(http, FakeMojang(http, ["1.21.1"]))
    assert loader.latest_version("1.21.1") == "21.1.77"
    assert loader.latest_version("1.21.4") is None


def test_fabric_latest_version(http):
    http.json[f"{FABRIC_META}/versions/loader/1.21.1"] = [
        {"loader": {"version": "0.17.0-beta", "stable": False}},
        {"loader": {"version": "0.16.10", "stable": True}},
    ]
    http.json[f"{FABRIC_META}/versions/loader/26.1"] = []
    loader = FabricLoader(http, FakeMojang(http, ["1.21.1"]))
    assert loader.latest_version("1.21.1") == "0.16.10"
    assert loader.latest_version("26.1") is None
    assert loader.latest_version("9.9") is None  # 404 means unsupported


def test_version_key_orders_prereleases_first():
    assert sorted(["1.2.0", "1.10.0", "1.2.0-beta", "1.9"], key=version_key) == \
        ["1.2.0-beta", "1.2.0", "1.9", "1.10.0"]


def test_mojang_ordering_handles_new_version_scheme(http):
    mojang = FakeMojang(http, ["1.21.9", "1.21.10", "26.1"])
    assert mojang.newer_than("1.21.9") == ["26.1", "1.21.10"]
    assert mojang.latest_release() == "26.1"


def test_log_patterns():
    assert READY.search('[12:00:00] [Server thread/INFO]: Done (4.512s)! For help, type "help"')
    assert READY.search('[12:00:00] [Server thread/INFO] [minecraft/DedicatedServer]: Done (31.2s)! For help')
    assert not READY.search("<steve> Done (1s)! lol")
    assert PLAYERS.search("[Server thread/INFO]: There are 3 of a max of 20 players online: a, b, c").group(1) == "3"


def test_rcon_packets():
    packet = encode(7, 2, "list")
    assert int.from_bytes(packet[:4], "little") == len(packet) - 4
    assert decode(packet[4:]) == (7, 2, "list")


def test_backup_restore_prune(tmp_path):
    server = tmp_path / "server"
    (server / "world").mkdir(parents=True)
    (server / "world" / "level.dat").write_text("v1")
    (server / "logs").mkdir()
    (server / "logs" / "latest.log").write_text("noise")
    backups = tmp_path / "backups"

    archive = backup.create(server, backups, "test", ["logs"])
    (server / "world" / "level.dat").write_text("v2")
    (server / "new.txt").write_text("x")
    backup.restore(archive, server)
    assert (server / "world" / "level.dat").read_text() == "v1"
    assert not (server / "new.txt").exists()
    assert not (server / "logs").exists()

    for i in range(3):
        backup.create(server, backups, f"extra{i}", [])
    backup.prune(backups, 2)
    assert len(backup.list_backups(backups)) == 2


def test_memory_setting(tmp_path, monkeypatch):
    def memory(value):
        return configmod.parse(tmp_path, {"server": {"loader": "fabric", "memory": value}}).server.memory
    assert memory("4g") == "4G" and memory("4096M") == "4096M" and memory("AUTO") == "auto"
    for bad in ("4", "4 GB", "-1G", "0G", "lots"):
        with pytest.raises(ConfigError, match="server.memory"):
            memory(bad)

    # "auto" becomes a real size on the java command line, never -Xmxauto.
    from mcsm import setup as setupmod
    from mcsm.lock import Lock
    from mcsm.manager import Manager
    monkeypatch.setattr(setupmod, "suggested_memory_gb", lambda total=None: 6)
    m = Manager.__new__(Manager)
    m.config = configmod.parse(tmp_path, {"server": {"loader": "fabric", "memory": "auto"}})
    argv = m.launch_argv(Lock(minecraft="1.21.1", launch=["-jar", "server.jar"]), java="java")
    assert argv[:3] == ["java", "-Xms6G", "-Xmx6G"]


def test_web_auth_store(tmp_path):
    from mcsm import webauth
    cfg = configmod.parse(tmp_path, {"server": {"loader": "fabric"}})
    store = webauth.AuthStore(cfg)
    auth = store.get()
    assert auth.default and auth.check("PASSWORD") and auth.check("Password") and not auth.check("passwort")
    store.set("pin", "0042")
    assert webauth.AuthStore(cfg).get().check("0042")  # saved
    for mode, secret in (("pin", "123"), ("pin", "123456789"), ("password", "abc"), ("password", "PASSWORD"),
                         ("magic", "x")):
        with pytest.raises(ConfigError):
            store.set(mode, secret)
    assert store.get().mode == "none" or store.get().check("0042")  # failed changes keep the old one

    # mcsm 0.1's generated plain-text password is replaced by the default PASSWORD.
    legacy = tmp_path / "old"
    (legacy / ".mcsm").mkdir(parents=True)
    (legacy / ".mcsm" / "web-password").write_text("s3cret-from-0.1\n")
    old = webauth.AuthStore(configmod.parse(legacy, {"server": {"loader": "fabric"}})).get()
    assert old.check("PASSWORD") and old.default and not old.check("s3cret-from-0.1")
    assert not (legacy / ".mcsm" / "web-password").exists()

    # [web] password in mcsm.toml wins and can't be changed from the UI.
    cfg.web.password = "from-config"
    assert store.get().managed and store.get().check("from-config")
    with pytest.raises(ConfigError, match="mcsm.toml"):
        store.set("pin", "1234")
    cfg.web.password = ""
    assert store.get().check("0042") and not store.get().managed


def test_host_allowlist():
    import socket
    from mcsm.web import host_allowed
    for ok in ("localhost:8765", "127.0.0.1:8765", "[::1]:8765", "10.0.0.5", "pc.local", "a.localhost",
               socket.gethostname(), None, "proxy.example.com"):
        assert host_allowed(ok, ["proxy.example.com"]), ok
    for bad in ("evil.example", "evil.example:8765", "localhost.evil.example", "127.0.0.1.nip.io"):
        assert not host_allowed(bad, []), bad


def test_http_waits_out_rate_limits(monkeypatch):
    import io
    import urllib.error
    from email.message import Message
    from mcsm import http as httpmod

    def limited(retry_after=None):
        headers = Message()
        if retry_after:
            headers["Retry-After"] = retry_after
        return urllib.error.HTTPError("https://api.mojang.com/x", 429, "Too Many Requests", headers, io.BytesIO())

    replies = [limited("12"), limited(), limited(), limited(), io.BytesIO(b'{"id": "abc"}')]
    slept = []
    monkeypatch.setattr(httpmod.time, "sleep", slept.append)

    def urlopen(req, timeout):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        reply.headers = {}
        return reply
    monkeypatch.setattr(httpmod.urllib.request, "urlopen", urlopen)
    assert httpmod.HttpClient(cache_ttl=0).get_json("https://api.mojang.com/x") == {"id": "abc"}
    assert slept == [12, 10, 20, 30]  # Retry-After first, then growing waits

    # Other client errors still fail at once, and plain failures stop after the normal retries.
    replies[:] = [urllib.error.HTTPError("u", 404, "nope", Message(), io.BytesIO())]
    with pytest.raises(httpmod.HttpError):
        httpmod.HttpClient(cache_ttl=0).get_json("https://api.mojang.com/y")
