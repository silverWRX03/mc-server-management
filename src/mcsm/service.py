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


PANEL_UNIT = "mcsm.service"


def plan(root: Path, system: bool | None = None, home: Path | None = None, panel: bool = False) -> Plan:
    """The unit for one server (``mcsm run --web`` in ``root``), or with ``panel`` for the whole
    control panel (``mcsm start``: every server in the mcsm folder ``root``)."""
    if system is None:
        system = getattr(os, "geteuid", lambda: -1)() == 0
    name = PANEL_UNIT if panel else unit_name(root)
    if system:
        path = Path("/etc/systemd/system") / name
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (home or Path.home()) / ".config")
        path = base / "systemd" / "user" / name
    args = ["start", "--no-browser", "--web-host", "0.0.0.0"] if panel else ["run", "--web"]
    exec_start = " ".join(_quote(a) for a in [*mcsm_command(), *args])
    what = f"mcsm control panel and servers ({root})" if panel else f"Minecraft server managed by mcsm ({root})"
    env = f"Environment=MCSM_HOME={_quote(str(root))}\n" if panel else ""
    remove = "mcsm service uninstall --panel" if panel else "mcsm service uninstall"
    text = f"""\
# Installed by `mcsm service install`. Remove with `{remove}`.
[Unit]
Description={what}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
{env}ExecStart={exec_start}
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


def install(root: Path, runner: Runner = subprocess.run, system: bool | None = None, panel: bool = False) -> list[str]:
    """Write and start the service. Returns messages for the user."""
    if not supported():
        raise ServiceError("services are set up with systemd, which this system doesn't have; "
                           "run `mcsm start --no-browser` yourself (e.g. in tmux or screen) instead")
    p = plan(root, system, panel=panel)
    if not panel:
        # The service can't answer a prompt, so record the acceptance for this server. (The
        # control panel shows the notice at the first sign-in instead.)
        notice.accept(root, by="service")
    root.mkdir(parents=True, exist_ok=True)
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


def uninstall(root: Path, runner: Runner = subprocess.run, system: bool | None = None, panel: bool = False) -> str:
    p = plan(root, system, panel=panel)
    if not p.path.exists():
        raise ServiceError(f"no service installed for this server ({p.path})")
    _run(runner, [*p.systemctl, "disable", "--now", p.name], check=False)
    p.path.unlink()
    _run(runner, [*p.systemctl, "daemon-reload"], check=False)
    return f"removed {p.name}"


def status(root: Path, runner: Runner = subprocess.run, system: bool | None = None, panel: bool = False) -> str:
    p = plan(root, system, panel=panel)
    if not p.path.exists():
        return f"no service installed for this server (would be {p.name})"
    return _run(runner, [*p.systemctl, "status", "--no-pager", p.name], check=False).stdout
