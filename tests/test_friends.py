"""Friend downloads: the client pack, the share server, and `mcsm join` setting up a launcher."""

import hashlib
import json
import socket
import threading
import urllib.request

import pytest

from mcsm import config as configmod, join, nbt, selfupdate, setup as setupmod
from mcsm.clientpack import PackBuilder, allowed_url
from mcsm.config import ModSpec
from mcsm.hub import Hub
from mcsm.loaders.fabric import FABRIC_META

from test_hub import login
from test_manager import manager, update
from test_web import Client, wait_for


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ pieces
def test_servers_dat_round_trip():
    data = nbt.add_server(None, "Weekend Survival", "mc.example.com")
    data = nbt.add_server(data, "Other", "10.0.0.2:25570")
    data = nbt.add_server(data, "Weekend Survival (renamed)", "mc.example.com")
    servers = nbt.loads(data)["servers"]
    assert [(s["name"], s["ip"]) for s in servers] == [("Weekend Survival (renamed)", "mc.example.com"),
                                                      ("Other", "10.0.0.2:25570")]
    # Entries (and tag types) mcsm doesn't know about survive.
    root = {"servers": nbt.ListTag(nbt.COMPOUND, [{"name": "Old", "ip": "old:1", "icon": "abc",
                                                  "hidden": nbt.Tagged(nbt.BYTE, 1)}]),
            "other": nbt.Tagged(nbt.LONG, 2 ** 40), "arr": nbt.Tagged(nbt.INT_ARRAY, [1, -2])}
    back = nbt.loads(nbt.add_server(nbt.dumps(root), "New", "new:2"))
    assert back["servers"][1]["hidden"] == nbt.Tagged(nbt.BYTE, 1) and back["other"].value == 2 ** 40
    assert back["arr"].value == [1, -2]
    with pytest.raises(nbt.NBTError):
        nbt.loads(b"\x0a\x00")


def test_invites():
    inv = join.Invite("mc.example.com", 8766, "A" * 24)
    assert join.parse_invite(inv.url) == inv
    assert join.parse_invite(inv.code) == inv
    name = join.download_name("Weekend Survival! <3", inv, "mcsm-windows-x64.exe")
    assert name.endswith(").exe") and "Weekend Survival" in name and "<" not in name
    assert join.invite_from_name(name) == inv
    assert join.invite_from_name(name.replace(".exe", " (1).exe")) == inv  # browsers add " (1)"
    assert join.invite_from_name("mcsm-windows-x64.exe") is None
    ipv6 = join.Invite("2001:db8::1", 8766, "B" * 24)
    assert join.parse_invite(ipv6.url) == ipv6 and join.parse_invite(ipv6.code) == ipv6
    for bad in ("hello", "http://x/other/abc", "http://x:8766/join/short", "ftp://x/join/" + "A" * 24):
        with pytest.raises(join.JoinError):
            join.parse_invite(bad)


def pack(**over):
    p = {"format": 1, "name": "Weekend Survival", "minecraft": "1.21.1", "loader": "fabric",
         "loader_version": "0.16.5", "java_major": 21, "address": "mc.example.com", "memory_gb": 4,
         "mods": [], "manual": [], "skipped": [], "icon": None}
    p.update(over)
    return p


def test_packs_are_checked_before_use():
    assert join.validate_pack(pack())
    mod = {"name": "X", "filename": "x.jar", "url": "https://cdn.modrinth.com/data/x/x.jar", "sha1": "a" * 40}
    assert join.validate_pack(pack(mods=[mod]))
    for bad in (pack(format=2), pack(loader="bukkit"), pack(minecraft="1.21; rm"), pack(address="a b"),
                pack(mods=[{**mod, "url": "https://evil.example/x.jar"}]),
                pack(mods=[{**mod, "url": "http://cdn.modrinth.com/x.jar"}]),
                pack(mods=[{**mod, "filename": "../x.jar"}]), pack(mods=[{**mod, "filename": "x.exe"}]),
                pack(mods=[{**mod, "sha1": None}]), "nope"):
        with pytest.raises(join.JoinError):
            join.validate_pack(bad)
    assert allowed_url("https://edge.forgecdn.net/files/1/2/a.jar") and not allowed_url("https://cdn.modrinth.com.evil/x")


# -------------------------------------------------------------- the pack
@pytest.fixture
def modded(make_config, http, modrinth):
    modrinth.project("AAA", "goodmod", "Good Mod")                                   # both sides
    modrinth.version("AAA", "1.0", ["1.21.1"])
    modrinth.project("SRV", "servermod", "Server Tool", client_side="unsupported")   # server only
    modrinth.version("SRV", "1.0", ["1.21.1"])
    modrinth.project("MAP", "minimap", "Mini Map", server_side="unsupported")        # players only
    modrinth.version("MAP", "2.0", ["1.21.1"], deps=["LIB"])
    modrinth.project("LIB", "maplib", "Map Library", server_side="unsupported")
    modrinth.version("LIB", "1.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "goodmod"), ModSpec("modrinth", "servermod")])
    configmod.set_value(cfg.path, "client", "mods", '["minimap", "nosuchmod"]')
    m = manager(configmod.load(cfg.root), http, ["1.21.1"])
    assert update(m).ok
    return m


def test_client_pack_has_what_players_need(modded):
    p = PackBuilder(modded).build("mc.example.com")
    names = {x["name"]: x["side"] for x in p["mods"]}
    assert names == {"Good Mod": "both", "Mini Map": "client", "Map Library": "client"}  # not the server tool
    assert p["minecraft"] == "1.21.1" and p["loader"] == "fabric" and p["address"] == "mc.example.com"
    assert [s["name"] for s in p["skipped"]] == ["nosuchmod"]
    assert join.validate_pack(p)


# ---------------------------------------------------------- joining
@pytest.fixture
def launcher(tmp_path):
    mc = tmp_path / ".minecraft"
    mc.mkdir()
    (mc / "launcher_profiles.json").write_text(json.dumps({"profiles": {"x": {"name": "Mine", "type": "custom"}},
                                                            "settings": {"keep": True}}))
    return mc


def test_join_sets_up_the_launcher(launcher, http):
    jar = b"mod bytes"
    http.files["https://cdn.modrinth.com/data/A/a.jar"] = jar
    http.json[f"{FABRIC_META}/versions/loader/1.21.1/0.16.5/profile/json"] = {
        "id": "fabric-loader-0.16.5-1.21.1", "inheritsFrom": "1.21.1", "mainClass": "net.fabricmc.Main"}
    mod = {"name": "A", "filename": "a.jar", "url": "https://cdn.modrinth.com/data/A/a.jar",
           "sha1": hashlib.sha1(jar).hexdigest(), "side": "both"}
    j = join.Joiner(join.Invite("mc.example.com", 8766, "A" * 24), mc_dir=launcher, http=http, say=lambda s: None)
    result = j.run(pack(mods=[mod]), open_launcher=False)
    game = launcher / "mcsm" / "weekend-survival"
    assert result["downloaded"] == 1 and (game / "mods" / "a.jar").read_bytes() == jar
    assert (launcher / "versions" / "fabric-loader-0.16.5-1.21.1" / "fabric-loader-0.16.5-1.21.1.json").exists()
    ours = json.loads((launcher / "versions" / "mcsm-weekend-survival" / "mcsm-weekend-survival.json").read_text())
    assert ours["inheritsFrom"] == "fabric-loader-0.16.5-1.21.1" and ours["jar"] == "1.21.1"
    assert ours["arguments"]["game"] == ["--quickPlayMultiplayer", "mc.example.com"]  # joins by itself
    assert nbt.loads((game / "servers.dat").read_bytes())["servers"][0]["ip"] == "mc.example.com"
    profiles = json.loads((launcher / "launcher_profiles.json").read_text())
    ours = profiles["profiles"]["mcsm-weekend-survival"]
    assert ours["name"] == "Weekend Survival" and ours["lastVersionId"] == "mcsm-weekend-survival"
    assert ours["gameDir"] == str(game) and "-Xmx4G" in ours["javaArgs"]
    assert profiles["profiles"]["x"]["name"] == "Mine" and profiles["settings"] == {"keep": True}  # untouched

    # Running it again after the server dropped the mod removes it, but not jars the player added.
    (game / "mods" / "mine.jar").write_bytes(b"theirs")
    result = j.run(pack(mods=[], minecraft="1.19.2", loader="vanilla", loader_version=None), open_launcher=False)
    assert result["removed"] == 1 and not (game / "mods" / "a.jar").exists() and (game / "mods" / "mine.jar").exists()
    ours = json.loads((launcher / "versions" / "mcsm-weekend-survival" / "mcsm-weekend-survival.json").read_text())
    assert ours["inheritsFrom"] == "1.19.2" and "arguments" not in ours  # no quick play before 1.20


def test_join_needs_the_minecraft_launcher(tmp_path, http):
    j = join.Joiner(join.Invite("h", 1, "A" * 24), mc_dir=tmp_path / "none", http=http, say=lambda s: None)
    with pytest.raises(join.JoinError, match="Minecraft Launcher"):
        j.run(pack(), open_launcher=False)


def test_join_rejects_a_tampered_mod(launcher, http):
    http.files["https://cdn.modrinth.com/data/A/a.jar"] = b"something else"
    mod = {"name": "A", "filename": "a.jar", "url": "https://cdn.modrinth.com/data/A/a.jar", "sha1": "0" * 40}
    j = join.Joiner(join.Invite("h", 1, "A" * 24), mc_dir=launcher, http=http, say=lambda s: None)
    with pytest.raises(join.JoinError, match="checksum"):
        j.run(pack(loader="vanilla", mods=[mod]), open_launcher=False)
    assert not (launcher / "mcsm" / "weekend-survival" / "mods" / "a.jar").exists()


def test_join_runs_the_neoforge_installer(launcher, http):
    http.files["https://maven.neoforged.net/releases/net/neoforged/neoforge/21.1.1/neoforge-21.1.1-installer.jar"] = b"jar"
    ran = []

    def run(cmd, cwd, capture_output, text, **kw):  # kw: creationflags on Windows
        ran.append(cmd)
        vdir = launcher / "versions" / "neoforge-21.1.1"
        vdir.mkdir(parents=True)
        (vdir / "neoforge-21.1.1.json").write_text("{}")
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    j = join.Joiner(join.Invite("h", 1, "A" * 24), mc_dir=launcher, http=http, say=lambda s: None, run=run,
                    java_probe=lambda binary: 21)
    j.run(pack(loader="neoforge", loader_version="21.1.1"), open_launcher=False)
    assert ran[0][0] == "java" and ran[0][-2:] == ["--installClient", str(launcher)]
    ours = json.loads((launcher / "versions" / "mcsm-weekend-survival" / "mcsm-weekend-survival.json").read_text())
    assert ours["inheritsFrom"] == "neoforge-21.1.1"


# ------------------------------------------------------- share server
def test_friends_page_and_download(tmp_path, http, modrinth, fake_template, monkeypatch):
    modrinth.project("FAPI", "fabric-api", "Fabric API")
    modrinth.version("FAPI", "0.1", ["1.21.1"])
    modrinth.project("AAA", "goodmod", "Good Mod")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    home = tmp_path / "home"
    root = home / "servers" / "survival"
    setupmod.configure(root, setupmod.SetupSpec.from_dict({"loader": "fabric", "minecraft": "1.21.1", "mods": ["goodmod"],
                                                           "motd": "Weekend Survival", "accept_eula": True,
                                                           "port": 25570, "friends": True}))
    assert configmod.load(root).client.enabled and configmod.load(root).client.token
    assert update(manager(configmod.load(root), http, ["1.21.1"])).ok

    hub = Hub(home, make_manager=lambda cfg: manager(cfg, http, ["1.21.1"]), http=http, tick=0.1)
    hub.web.port = 0
    share_port = free_port()
    hub._save_hub_file({"share": {"port": share_port, "address": ""}})
    # A download of mcsm for this computer (normally the running executable or GitHub's).
    asset = selfupdate.asset_name()
    fake_exe = hub.state_dir / "downloads" / f"{selfupdate.__version__}-{asset}"
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_bytes(b"MZ fake mcsm")
    t = threading.Thread(target=hub.run, daemon=True)
    t.start()
    try:
        wait_for(lambda: hub.ui is not None and hub.share is not None and hub.share.httpd is not None)
        c = Client(hub.ui.url.rstrip("/"))
        login(c)
        info = c.get("/api/servers/survival/client")[1]
        assert info["enabled"] and info["share"]["running"] and info["link"].endswith(configmod.load(root).client.token)
        assert {m["name"] for m in info["pack"]["mods"]} == {"Fabric API", "Good Mod"}

        token = configmod.load(root).client.token
        base = f"http://127.0.0.1:{share_port}/join/{token}"
        with urllib.request.urlopen(base) as r:
            page = r.read().decode()
            assert "Weekend Survival" in page and r.headers["Content-Security-Policy"].startswith("default-src 'none'")
        with urllib.request.urlopen(base + "/pack.json") as r:
            p = json.loads(r.read())
        assert p["address"] == "127.0.0.1:25570" and len(p["mods"]) == 2
        os_key = {"mcsm-windows-x64.exe": "windows", "mcsm-macos-arm64": "macos",
                  "mcsm-linux-x64": "linux", "mcsm-linux-arm64": "linux-arm64"}[asset]
        with urllib.request.urlopen(f"{base}/download/{os_key}") as r:
            assert r.read() == b"MZ fake mcsm"
            disposition = r.headers["Content-Disposition"]
        filename = disposition.split('filename="')[1].split('"')[0]
        assert join.invite_from_name(filename) == join.Invite("127.0.0.1", share_port, token)

        # A friend's copy fetches the same pack through the invite.
        assert join.Joiner(join.Invite("127.0.0.1", share_port, token), mc_dir=tmp_path / "x").fetch_pack()["name"] \
            == "Weekend Survival"
        for bad in (f"http://127.0.0.1:{share_port}/join/{'Z' * 24}", f"http://127.0.0.1:{share_port}/",
                    f"{base}/download/solaris"):
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(bad)
            assert e.value.code == 404

        # Your own files for players come from the share server, and only from there.
        from mcsm.clientpack import client_dir
        client_dir(configmod.load(root)).mkdir()
        (client_dir(configmod.load(root)) / "My Tweaks-1.0.jar").write_bytes(b"homemade")
        assert c.get("/api/servers/survival/client")[1]["local_mods"] == ["My Tweaks-1.0.jar"]
        with urllib.request.urlopen(base + "/pack.json") as r:
            local = next(m for m in json.loads(r.read())["mods"] if m.get("local"))
        assert local["url"] == f"{base}/mods/My%20Tweaks-1.0.jar"
        with urllib.request.urlopen(local["url"]) as r:
            assert r.read() == b"homemade"
        fetched = join.Joiner(join.Invite("127.0.0.1", share_port, token), mc_dir=tmp_path / "y").fetch_pack()
        assert any(m.get("local") for m in fetched["mods"])
        with pytest.raises(join.JoinError):  # someone else's address for "your own" file: refused
            join.validate_pack({**fetched, "mods": [{**local, "url": "http://evil.example/x.jar"}]}, base)
        assert c.post("/api/servers/survival/client/local/remove", {"name": "My Tweaks-1.0.jar"})[0] == 200
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(local["url"])

        # Client-only mods and a new link, from the Friends page.
        modrinth.project("MAP", "minimap", "Mini Map", server_side="unsupported")
        modrinth.version("MAP", "2.0", ["1.21.1"])
        info = c.post("/api/servers/survival/client", {"mods": ["minimap"], "memory_gb": 6})[1]
        assert {m["name"] for m in info["pack"]["mods"]} == {"Fabric API", "Good Mod", "Mini Map"}
        assert info["pack"]["memory_gb"] == 6
        c.post("/api/servers/survival/client/new-link", {})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/pack.json")  # the old link stops working
        assert c.post("/api/hub/share", {"port": 80})[0] == 400

        # Switching it off stops sharing.
        c.post("/api/servers/survival/client", {"enabled": False})
        wait_for(lambda: hub.share is None, timeout=15)
    finally:
        hub.stop_requested.set()
        t.join(30)


def test_join_command_line(monkeypatch, capsys, launcher, http):
    from mcsm import cli
    got = []
    monkeypatch.setattr(join, "run_interactive", lambda invite, confirm, open_launcher, **kw: got.append(invite) or 0)
    code = "A" * 24
    assert cli.main(["join", f"http://mc.example.com:8766/join/{code}", "--yes"]) == 0
    assert got[0] == join.Invite("mc.example.com", 8766, code)
    assert cli.main(["join", "not an invite", "--yes"]) == 2


def test_client_side_companions_of_server_mods_are_included(make_config, http, modrinth):
    """A server mod that needs a client-only mod: the server skips it, players get it."""
    modrinth.project("VOX", "voicechat-server", "Voice Server")
    modrinth.version("VOX", "1.0", ["1.21.1"], deps=["VCC"])
    modrinth.project("VCC", "voicechat-client", "Voice Client", server_side="unsupported")
    modrinth.version("VCC", "1.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "voicechat-server")])
    m = manager(cfg, http, ["1.21.1"])
    assert update(m).ok
    assert [x.name for x in m.lock.mods] == ["Voice Server"]
    p = PackBuilder(m).build("mc.example.com")
    extra = next(x for x in p["mods"] if x["name"] == "Voice Client")
    assert extra["side"] == "client" and extra["needed_by"] == "Voice Server"


def test_player_mod_search_leaves_out_server_only_mods(http):
    from mcsm.browse import Browser, BrowseError
    from mcsm.mods.modrinth import API
    seen = []
    http.json[f"{API}/search"] = {"total_hits": 0, "hits": []}
    orig = http.get_json
    http.get_json = lambda url, params=None, headers=None, cache=True: seen.append(params) or orig(url, params, headers)
    Browser(http).search("modrinth", "mod", "map", loader="fabric", side="client")
    assert ["client_side:required", "client_side:optional"] in json.loads(seen[-1]["facets"])
    with pytest.raises(BrowseError):
        Browser(http, "key").search("curseforge", "mod", "map", loader="fabric", side="client")


def test_new_server_form_takes_friends_mods_and_files(tmp_path):
    spec = setupmod.SetupSpec.from_dict({"loader": "fabric", "accept_eula": True,
                                         "client_mods": ["minimap", "minimap", "jei"], "client_local": ["ab" * 8]})
    assert spec.friends  # picking mods for friends makes their download
    assert spec.client_mods == ["minimap", "jei"] and spec.client_local == ["ab" * 8]
    root = tmp_path / "srv"
    setupmod.configure(root, spec)
    cfg = configmod.load(root)
    assert cfg.client.enabled and cfg.client.mods == ["minimap", "jei"]
    for bad in ({"client_mods": ["curseforge:123"]}, {"client_mods": ["../x"]}, {"client_local": ["nope"]}):
        with pytest.raises(configmod.ConfigError):
            setupmod.SetupSpec.from_dict({"loader": "fabric", "accept_eula": True, **bad})
