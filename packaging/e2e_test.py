"""End-to-end test of a built mcsm executable against the real services.

Unlike the unit tests (which fake every server) this downloads real Minecraft, a real
mod loader, real mods and a real Java, boots a real server, and drives it through
the web UI, RCON and the command line. It needs internet access and a few GB of RAM.

    python packaging/e2e_test.py dist/mcsm --loader fabric --minecraft 1.21.1 \\
        --mod fabric-api --mod lithium --upgrade

The Minecraft EULA (https://aka.ms/MinecraftEULA) is accepted for the test server.
On failure, logs are copied to ./e2e-logs for the CI artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 8793
LOGS = Path("e2e-logs")


class Failed(Exception):
    pass


def step(title: str) -> None:
    print(f"\n=== {title}", flush=True)


def run(exe: str, *args: str, env: dict, timeout: int = 600, ok: bool = True) -> str:
    t0 = time.time()
    out = subprocess.run([exe, *args], env=env, capture_output=True, text=True, timeout=timeout,
                         stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    text = (out.stdout + out.stderr).strip()
    print(f"$ mcsm {' '.join(args)}  ({time.time() - t0:.0f}s, exit {out.returncode})")
    print("\n".join("  " + line for line in text.splitlines()[-40:]))
    if ok and out.returncode != 0:
        raise Failed(f"`mcsm {' '.join(args)}` exited with {out.returncode}")
    return text


class Web:
    def __init__(self, password: str):
        self.cookie = ""
        self.call("POST", "/api/login", {"password": password})

    def call(self, method: str, path: str, body: dict | None = None):
        headers = {"X-MCSM": "1", "Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            if "Set-Cookie" in r.headers:
                self.cookie = r.headers["Set-Cookie"].split(";")[0]
            return json.loads(r.read() or b"{}")


def wait(what: str, fn, timeout: float, every: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (OSError, urllib.error.URLError, ValueError) as e:
            last = e
        time.sleep(every)
    raise Failed(f"timed out waiting for {what}" + (f" (last error: {last})" if last else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("exe")
    ap.add_argument("--loader", required=True)
    ap.add_argument("--minecraft", default="latest")
    ap.add_argument("--mod", action="append", default=[])
    ap.add_argument("--memory", default="3G")
    ap.add_argument("--upgrade", action="store_true", help="also run `mcsm update` to the newest compatible version")
    args = ap.parse_args()

    exe = str(Path(args.exe).resolve())
    work = Path(tempfile.mkdtemp(prefix="mcsm-e2e-"))
    root = work / "server-root"
    env = {**os.environ, "XDG_CONFIG_HOME": str(work / "cfg"), "APPDATA": str(work / "cfg")}
    daemon = None
    try:
        step(f"create a {args.loader} server (Minecraft {args.minecraft}, mods: {', '.join(args.mod) or 'none'})")
        mods = [x for m in args.mod for x in ("--mod", m)]
        run(exe, "--accept-notice", "create", str(root), "--loader", args.loader, "--minecraft", args.minecraft,
            *mods, "--memory", args.memory, "--port", "25599", "--rcon", "--accept-eula", "-q",
            env=env, timeout=1800)
        lock = json.loads((root / "mcsm.lock.json").read_text())
        print(f"installed: Minecraft {lock['minecraft']}, {lock['loader']} {lock['loader_version']}, "
              f"Java {lock['java_major']}, {len(lock['mods'])} mod file(s)")
        if len(lock["mods"]) < len(args.mod):
            raise Failed(f"expected at least {len(args.mod)} mods, got {[m['name'] for m in lock['mods']]}")
        for m in lock["mods"]:
            if not (root / "server" / "mods" / m["filename"]).is_file():
                raise Failed(f"{m['filename']} is missing from mods/")
        run(exe, "-C", str(root), "java", "list", env=env)

        step("check for updates")
        run(exe, "-C", str(root), "check", env=env, timeout=300)

        if args.upgrade:
            step("upgrade to the newest compatible Minecraft (backup, swap, test boot)")
            before = lock["minecraft"]
            run(exe, "-C", str(root), "update", "-y", "-q", env=env, timeout=1800)
            after = json.loads((root / "mcsm.lock.json").read_text())
            print(f"Minecraft {before} -> {after['minecraft']} ({len(after['mods'])} mod files)")
            if after["minecraft"] == before:
                print("(already on the newest compatible version)")

        step("run the server with the web UI")
        log = open(work / "run.log", "w", encoding="utf-8")
        daemon = subprocess.Popen([exe, "-C", str(root), "run", "--web", "--web-port", str(PORT)], env=env,
                                  stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        pw_file = root / ".mcsm" / "web-password"
        wait("the web UI", lambda: pw_file.exists() and Web(pw_file.read_text().strip()), 120)
        web = Web(pw_file.read_text().strip())
        status = wait("the server to finish starting", lambda: (s := web.call("GET", "/api/status"))["state"] == "running" and s, 900, 5)
        print(f"web UI: {status['state']}, Minecraft {status['minecraft']}, {status['mods']} mods")

        web.call("POST", "/api/command", {"command": "list"})
        wait("`list` output in the web console",
             lambda: any("players online" in line["text"] for line in web.call("GET", "/api/console?since=0")["lines"]), 60)
        print("web console works")
        for path in ("/api/mods", "/api/players", "/api/java", "/api/backups", "/api/settings", "/api/licenses"):
            web.call("GET", path)
        print("web API pages load")

        step("RCON: console command and op a player while running")
        out = run(exe, "-C", str(root), "cmd", "list", env=env)
        if "players online" not in out:
            raise Failed("RCON `list` gave no player count")
        run(exe, "-C", str(root), "player", "op", "Notch", env=env)
        wait("ops.json to list Notch",
             lambda: "Notch" in (root / "server" / "ops.json").read_text(), 60)
        print("op via RCON works")

        step("stop cleanly")
        run(exe, "-C", str(root), "stop", env=env)
        daemon.wait(timeout=300)
        print(f"daemon exited with {daemon.returncode}")
        if daemon.returncode != 0:
            raise Failed(f"mcsm run exited with {daemon.returncode}")
        daemon = None
        if "Stopping the server" not in (root / "server" / "logs" / "latest.log").read_text(errors="replace"):
            raise Failed("the Minecraft server didn't log a clean shutdown")

        step("manage players while stopped (Mojang UUID lookup)")
        run(exe, "-C", str(root), "player", "ban", "jeb_", "--reason", "e2e test", env=env)
        bans = json.loads((root / "server" / "banned-players.json").read_text())
        if not any(b["name"].lower() == "jeb_" for b in bans):
            raise Failed("jeb_ was not added to banned-players.json")
        run(exe, "-C", str(root), "player", "pardon", "jeb_", env=env)

        step("backups")
        run(exe, "-C", str(root), "backup", "--label", "e2e", env=env)
        listing = run(exe, "-C", str(root), "backup", "--list", env=env)
        if "e2e" not in listing:
            raise Failed("the backup isn't listed")

        print("\nEND-TO-END TEST PASSED")
        return 0
    except (Failed, subprocess.TimeoutExpired) as e:
        print(f"\nEND-TO-END TEST FAILED: {e}")
        return 1
    finally:
        if daemon is not None and daemon.poll() is None:
            try:
                run(exe, "-C", str(root), "stop", env=env, ok=False, timeout=60)
                daemon.wait(timeout=120)
            except Exception:
                daemon.kill()
        LOGS.mkdir(exist_ok=True)
        for src in (work / "run.log", root / "server" / "logs" / "latest.log", root / "mcsm.toml",
                    root / "mcsm.lock.json"):
            if src.exists():
                shutil.copy(src, LOGS / src.name)
        crash = root / "server" / "crash-reports"
        if crash.is_dir():
            shutil.copytree(crash, LOGS / "crash-reports", dirs_exist_ok=True)


if __name__ == "__main__":
    sys.exit(main())
