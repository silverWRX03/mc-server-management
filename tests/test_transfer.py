"""Moving a server to another computer: export to one .zip, import it there."""

import io
import json
import zipfile

import pytest

from mcsm import lock as lockmod, transfer
from mcsm.properties import read_properties

from test_hub import login
from test_web import wait_for


def test_export_and_import_through_the_web(hub_env):
    hub, c = hub_env
    login(c)
    d = hub.get("alpha")
    world = d.m.server_dir / "world"
    (world / "region").mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(b"level")
    (world / "session.lock").write_bytes(b"lock")
    (d.m.server_dir / "logs").mkdir(exist_ok=True)
    (d.m.server_dir / "logs" / "latest.log").write_text("noise")
    (d.m.config.state_dir / "java").mkdir(parents=True, exist_ok=True)

    assert c.post("/api/servers/alpha/export", {"backups": False})[0] == 200
    wait_for(lambda: d.last_job and d.last_job["name"] == "export", timeout=30)
    assert d.last_job["ok"], d.last_job
    exports = c.get("/api/servers/alpha/export")[1]["exports"]
    assert len(exports) == 1 and exports[0]["name"].startswith("Alpha ") and exports[0]["name"].endswith(".mcsm.zip")
    status, data, headers = c.get(f"/api/servers/alpha/export/download?name={exports[0]['name'].replace(' ', '%20')}")
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
    assert {"mcsm-export.json", "mcsm.toml", "mcsm.lock.json", "server/world/level.dat"} <= names
    assert not any(n.startswith(("server/logs/", ".mcsm")) or n.endswith("session.lock") for n in names)
    assert c.get("/api/servers/alpha/export/download?name=../mcsm.toml")[0] == 404

    # "The other computer": upload the export and import it.
    status, staged, _ = c.call("POST", "/api/hub/stage?filename=alpha.zip", raw=data)
    assert status == 200
    status, body, _ = c.post("/api/hub/import", {"id": staged["id"]})
    assert status == 200, body
    sid = body["id"]
    assert sid == "alpha-2"
    new = hub.get(sid)
    assert (new.m.server_dir / "world" / "level.dat").read_bytes() == b"level"
    assert new.m.lock.minecraft == "1.21.1" and new.m.config.server.dir == new.m.config.root / "server"
    # It used alpha's port, which is taken here, so it got another.
    assert read_properties(new.m.server_dir / "server.properties")["server-port"] != \
        read_properties(d.m.server_dir / "server.properties").get("server-port", "25565")
    assert new.state == "stopped"
    assert c.post("/api/hub/import", {"id": staged["id"]})[0] == 400  # used up

    assert c.post("/api/servers/alpha/export/delete", {"name": exports[0]["name"]})[0] == 200
    assert c.get("/api/servers/alpha/export")[1]["exports"] == []


def zip_with(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_import_is_careful(tmp_path):
    manifest = json.dumps({"format": 1, "name": "X"})
    bad = tmp_path / "bad.zip"
    bad.write_bytes(zip_with({"hello.txt": "hi"}))
    with pytest.raises(transfer.TransferError, match="isn't an mcsm server export"):
        transfer.read_manifest(bad)
    bad.write_bytes(b"not a zip")
    with pytest.raises(transfer.TransferError, match="isn't a .zip"):
        transfer.read_manifest(bad)
    bad.write_bytes(zip_with({"mcsm-export.json": json.dumps({"format": 99}), "mcsm.toml": ""}))
    with pytest.raises(transfer.TransferError, match="newer mcsm"):
        transfer.read_manifest(bad)

    # Paths outside the server (or mcsm's own files) are never written.
    evil = tmp_path / "evil.zip"
    evil.write_bytes(zip_with({"mcsm-export.json": manifest, "mcsm.toml": '[server]\nloader = "vanilla"\n',
                               "server/../../escaped.txt": "x", "/etc/passwd": "x", ".mcsm/web-auth.json": "{}",
                               "server/world/level.dat": "ok"}))
    root = tmp_path / "imported"
    transfer.import_into(evil, root)
    assert (root / "server" / "world" / "level.dat").read_text() == "ok"
    assert not (tmp_path / "escaped.txt").exists() and not (root / ".mcsm" / "web-auth.json").exists()
    with pytest.raises(transfer.TransferError, match="isn't empty"):
        transfer.import_into(evil, root)

    # A broken mcsm.toml: nothing is left behind.
    broken = tmp_path / "broken.zip"
    broken.write_bytes(zip_with({"mcsm-export.json": manifest, "mcsm.toml": "[server\nnot toml"}))
    with pytest.raises(transfer.TransferError):
        transfer.import_into(broken, tmp_path / "b")
    assert not (tmp_path / "b").exists()


def test_forge_launch_file_follows_the_os(tmp_path, monkeypatch):
    from mcsm.loaders import base
    root = tmp_path / "s"
    root.mkdir()
    lockmod.save(root, lockmod.Lock(minecraft="1.21.1", launch=["@libraries/net/neoforged/neoforge/21.1.1/win_args.txt", "nogui"]))
    monkeypatch.setattr(base, "args_file_name", lambda: "unix_args.txt")
    transfer._fix_launch(root)
    assert lockmod.load(root).launch[0].endswith("/unix_args.txt")
