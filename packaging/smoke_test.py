"""Smoke-test a built mcsm executable on the machine it was built for.

    python packaging/smoke_test.py dist/mcsm-linux-x64

Checks that it starts, that the bundled web UI is served, and that `mcsm stop`
shuts it down. The first-run notice is left unaccepted, so the daemon waits at
the notice and never downloads or starts a Minecraft server.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()


def run(exe: str, *args: str, env: dict, cwd: Path) -> str:
    out = subprocess.run([exe, *args], env=env, cwd=cwd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise SystemExit(f"`mcsm {' '.join(args)}` failed ({out.returncode}):\n{out.stdout}\n{out.stderr}")
    return out.stdout


def fetch(path: str) -> tuple[int, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return r.status, r.read().decode()


def main(exe: str) -> None:
    exe = str(Path(exe).resolve())
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "server-root"
        root.mkdir()
        setup_env = {**os.environ, "XDG_CONFIG_HOME": str(tmp / "cfg-a"), "APPDATA": str(tmp / "cfg-a")}
        fresh_env = {**os.environ, "XDG_CONFIG_HOME": str(tmp / "cfg-b"), "APPDATA": str(tmp / "cfg-b")}

        print(run(exe, "--version", env=setup_env, cwd=root).strip())
        assert "Apache-2.0" in run(exe, "licenses", env=setup_env, cwd=root)
        run(exe, "--accept-notice", "init", env=setup_env, cwd=root)
        assert (root / "mcsm.toml").exists()

        log = open(tmp / "run.log", "w")
        proc = subprocess.Popen([exe, "run", "--web", "--web-port", str(PORT)], env=fresh_env, cwd=root,
                                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            deadline = time.time() + 60
            while True:
                try:
                    status, body = fetch("/")
                    break
                except OSError:
                    if proc.poll() is not None or time.time() > deadline:
                        raise SystemExit("web UI never came up:\n" + (tmp / "run.log").read_text())
                    time.sleep(0.5)
            assert status == 200 and "mcsm" in body, body[:200]
            assert "use strict" in fetch("/app.js")[1]
            assert fetch("/style.css")[0] == 200
            print("web UI served")

            run(exe, "stop", env=fresh_env, cwd=root)
            proc.wait(timeout=60)
            print(f"stopped cleanly (exit {proc.returncode})")
        finally:
            if proc.poll() is None:
                # terminate() lets PyInstaller's launcher pass the signal on to the real process.
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
            log.close()
    print("smoke test passed")


if __name__ == "__main__":
    main(sys.argv[1])
