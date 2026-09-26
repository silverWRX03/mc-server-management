"""Mods' config files: found per mod, read and saved safely."""

import json
import zipfile

import pytest

from mcsm import configs

from test_hub import login


def jar(path, fabric_id=None, forge_ids=()):
    with zipfile.ZipFile(path, "w") as z:
        if fabric_id:
            z.writestr("fabric.mod.json", json.dumps({"id": fabric_id, "name": fabric_id.title()}))
        if forge_ids:
            z.writestr("META-INF/mods.toml", "modLoader=\"javafml\"\n" + "".join(
                f'[[mods]]\nmodId="{i}"\ndisplayName="{i.title()} Mod"\n' for i in forge_ids))


def server(tmp_path):
    sd = tmp_path / "server"
    (sd / "mods").mkdir(parents=True)
    jar(sd / "mods" / "lithium.jar", fabric_id="lithium")
    jar(sd / "mods" / "create.jar", forge_ids=["create", "create_sa"])
    (sd / "config" / "jei").mkdir(parents=True)
    (sd / "config" / "lithium.properties").write_text("mixin.ai=true\n")
    (sd / "config" / "create-server.toml").write_text("[kinetics]\nspeed = 1\n")
    (sd / "config" / "create_sa-common.toml").write_text("x = 1\n")
    (sd / "config" / "jei" / "jei-server.ini").write_text("[a]\nb=1\n")
    (sd / "config" / "notes.bin").write_bytes(b"\x00")
    (sd / "world" / "serverconfig").mkdir(parents=True)
    (sd / "world" / "serverconfig" / "create-server.toml").write_text("y = 2\n")
    return sd


def test_files_are_grouped_by_mod(tmp_path):
    sd = server(tmp_path)
    assert configs.jar_mods(sd / "mods" / "create.jar") == [("create", "Create Mod"), ("create_sa", "Create_Sa Mod")]
    g = configs.grouped(sd, [])
    by = {m["mod_id"]: m["files"] for m in g["mods"]}
    assert by["lithium"] == ["config/lithium.properties"]
    assert by["create"] == ["config/create-server.toml", "world/serverconfig/create-server.toml"]
    assert by["create_sa"] == ["config/create_sa-common.toml"]
    assert g["other"] == ["config/jei/jei-server.ini"]  # no JEI jar here; .bin isn't a config


def test_read_and_save_safely(tmp_path):
    sd = server(tmp_path)
    backups = tmp_path / "backups"
    f = configs.read(sd, "config/create-server.toml")
    assert f["format"] == "toml" and "speed" in f["text"]
    r = configs.write(sd, backups, "config/create-server.toml", "[kinetics]\nspeed = 2\n", f["modified"])
    assert (sd / "config" / "create-server.toml").read_text() == "[kinetics]\nspeed = 2\n"
    assert (backups / r["backup"]).read_text() == "[kinetics]\nspeed = 1\n"
    with pytest.raises(configs.ConfigFileError, match="changed on disk"):
        configs.write(sd, backups, "config/create-server.toml", "x", f["modified"] - 100)
    # Windows line endings stay Windows line endings.
    (sd / "config" / "lithium.properties").write_bytes(b"a=1\r\nb=2\r\n")
    configs.write(sd, backups, "config/lithium.properties", "a=1\nb=3\n")
    assert (sd / "config" / "lithium.properties").read_bytes() == b"a=1\r\nb=3\r\n"
    (sd / "config" / "x.json").write_text("{}")
    with pytest.raises(configs.ConfigFileError, match="valid JSON"):
        configs.write(sd, backups, "config/x.json", "{oops")
    for bad in ("../server.properties", "/etc/passwd", "config/../server.properties", "server.properties",
                "config/notes.bin", "mods/lithium.jar", "C:/x.toml"):
        with pytest.raises(configs.ConfigFileError):
            configs.read(sd, bad)


def test_config_editor_api(hub_env):
    hub, c = hub_env
    login(c)
    sd = hub.get("alpha").m.server_dir
    (sd / "config").mkdir(exist_ok=True)
    (sd / "config" / "goodmod.json").write_text('{"a": 1}')
    g = c.get("/api/servers/alpha/configs")[1]
    assert "config/goodmod.json" in g["other"]  # the fake jars declare no mod ids
    f = c.get("/api/servers/alpha/configs/file?path=config/goodmod.json")[1]
    assert f["text"] == '{"a": 1}' and f["format"] == "json"
    status, body, _ = c.post("/api/servers/alpha/configs/file", {"path": "config/goodmod.json", "text": '{"a": 2}',
                                                                  "modified": f["modified"]})
    assert status == 200 and not body["running"]
    assert c.post("/api/servers/alpha/configs/file", {"path": "config/goodmod.json", "text": "{"})[0] == 400
    assert c.get("/api/servers/alpha/configs/file?path=../mcsm.toml")[0] == 400
