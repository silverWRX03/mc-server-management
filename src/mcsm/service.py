"""Running mcsm as a systemd service on Linux, so the server starts at boot.

As a normal user this installs a *user* service (``~/.config/systemd/user``) and
enables "lingering" so it runs without anyone logged in. As root it installs a
system service. Either way it runs ``mcsm run --web`` in the server's folder.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import notice, selfupdate


class ServiceError(Exception):
    pass


def supported() -> bool:
    return sys.platform.startswith("linux") and shutil.which("systemctl") is not None


def unit_name(root: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", root.name).strip("-").lower() or "server"
    return f"mcsm-{slug}.service"


def _quote(arg: str) -> str:
    return f'"{arg}"' if re.search(r'[\s"\\]', arg) else arg


def mcsm_command() -> list[str]:
    """How to launch this same mcsm from a service."""
    if selfupdate.frozen():
        return [sys.executable]
    script = shutil.which("mcsm")
    return [script] if script else [sys.executable, "-m", "mcsm"]


@dataclass
class Plan:
    name: str
    path: Path
    system: bool
    text: str

    @property
    def systemctl(self) -> list[str]:
        return ["systemctl"] if self.system else ["systemctl", "--user"]


def plan(root: Path, system: bool | None = None, home: Path | None = None) -> Plan:
    if system is None:
        system = getattr(os, "geteuid", lambda: -1)() == 0
    name = unit_name(root)
    if system:
        path = Path("/etc/systemd/system") / name
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (home or Path.home()) / ".config")
        path = base / "systemd" / "user" / name
    exec_start = " ".join(_quote(a) for a in [*mcsm_command(), "run", "--web"])
    text = f"""\
# Installed by `mcsm service install`. Remove with `mcsm service uninstall`.
[Unit]
Description=Minecraft server managed by mcsm ({root})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={exec_start}
# mcsm stops the Minecraft server cleanly (saving the world) on SIGTERM.
KillSignal=SIGTERM
TimeoutStopSec=180
Restart=on-failure
RestartSec=30

[Install]
WantedBy={"multi-user.target" if system else "default.target"}
"""
    return Plan(name, path, system, text)


Runner = Callable[..., subprocess.CompletedProcess]


def _run(runner: Runner, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = runner(args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ServiceError(f"`{' '.join(args)}` failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def install(root: Path, runner: Runner = subprocess.run, system: bool | None = None) -> list[str]:
    """Write and start the service. Returns messages for the user."""
    if not supported():
        raise ServiceError("services are set up with systemd, which this system doesn't have")
    p = plan(root, system)
    # The service can't answer a prompt, so record the acceptance for this server.
    notice.accept(root, by="service")
    p.path.parent.mkdir(parents=True, exist_ok=True)
    p.path.write_text(p.text)
    _run(runner, [*p.systemctl, "daemon-reload"])
    _run(runner, [*p.systemctl, "enable", "--now", p.name])
    messages = [f"installed and started {p.name} ({p.path})"]
    if not p.system:
        user = getpass.getuser()
        linger = _run(runner, ["loginctl", "enable-linger", user], check=False)
        if linger.returncode != 0:
            messages.append(f"to keep it running after you log out, run: sudo loginctl enable-linger {user}")
    logs = "journalctl" + ("" if p.system else " --user") + f" -u {p.name} -f"
    messages.append(f"logs: {logs}")
    return messages


def uninstall(root: Path, runner: Runner = subprocess.run, system: bool | None = None) -> str:
    p = plan(root, system)
    if not p.path.exists():
        raise ServiceError(f"no service installed for this server ({p.path})")
    _run(runner, [*p.systemctl, "disable", "--now", p.name], check=False)
    p.path.unlink()
    _run(runner, [*p.systemctl, "daemon-reload"], check=False)
    return f"removed {p.name}"


def status(root: Path, runner: Runner = subprocess.run, system: bool | None = None) -> str:
    p = plan(root, system)
    if not p.path.exists():
        return f"no service installed for this server (would be {p.name})"
    return _run(runner, [*p.systemctl, "status", "--no-pager", p.name], check=False).stdout
