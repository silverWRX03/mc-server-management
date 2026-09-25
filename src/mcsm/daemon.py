"""``mcsm run``: keep the server up and keep it updated.

The daemon owns the server process. It restarts it after crashes, checks for
updates on a schedule (or when ``mcsm update`` drops a request file), and applies
them with an in-game countdown.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path

from .manager import Manager, ManualDownloadRequired
from .process import ServerProcess

log = logging.getLogger(__name__)

CRASH_WINDOW = 600      # seconds
MAX_CRASHES = 3         # within CRASH_WINDOW before giving up
EMPTY_RETRY = 300       # seconds between "is anyone online?" checks when waiting for an empty server


def pid_path(manager: Manager) -> Path:
    return manager.config.state_dir / "daemon.pid"


def request_path(manager: Manager) -> Path:
    return manager.config.state_dir / "update-requested"


def running_pid(manager: Manager) -> int | None:
    path = pid_path(manager)
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None
    except PermissionError:  # alive, owned by someone else
        return pid


class Daemon:
    def __init__(self, manager: Manager, tick: float = 2.0):
        self.m = manager
        self.tick = tick
        self.proc: ServerProcess | None = None
        self.stop_requested = threading.Event()
        self.crashes: deque[float] = deque()
        self.next_check = 0.0
        self.announced: set[str] = set()

    # ----------------------------------------------------------- lifecycle
    def run(self) -> int:
        state = self.m.config.state_dir
        state.mkdir(parents=True, exist_ok=True)
        if (pid := running_pid(self.m)) and pid != os.getpid():
            log.error("mcsm is already running (pid %s)", pid)
            return 1
        pid_path(self.m).write_text(str(os.getpid()))
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop_requested.set())
        try:
            return self._loop()
        finally:
            if self.proc and self.proc.running:
                log.info("stopping server")
                self.proc.stop(self.m.config.server.stop_timeout)
            pid_path(self.m).unlink(missing_ok=True)

    def _loop(self) -> int:
        if not self.m.lock.installed:
            log.info("no server installed yet; installing")
            self.check_for_updates(force=True)
            if not self.m.lock.installed:
                log.error("could not install a server; see the messages above")
                return 1
        if not self.proc or not self.proc.running:
            self.proc = self.m.start_server()
        self.m.notifier.send(f"Server is up (Minecraft {self.m.lock.minecraft})")
        self._forward_console()

        while not self.stop_requested.is_set():
            if not self.proc.running and not self.proc.stopping:
                if not self._handle_crash():
                    return 1
            req = request_path(self.m)
            requested = req.exists()
            if requested or time.monotonic() >= self.next_check:
                target = req.read_text().strip() or None if requested else None
                req.unlink(missing_ok=True)
                self.check_for_updates(force=requested, target=target)
            self.stop_requested.wait(self.tick)
        return 0

    def _handle_crash(self) -> bool:
        now = time.monotonic()
        self.crashes.append(now)
        while self.crashes and now - self.crashes[0] > CRASH_WINDOW:
            self.crashes.popleft()
        tail = "\n".join(self.proc.tail(15))
        if not self.m.config.restart_on_crash or len(self.crashes) > MAX_CRASHES:
            self.m.notifier.send(f"Server stopped unexpectedly (exit {self.proc.returncode}); not restarting.\n{tail}")
            return False
        self.m.notifier.send(f"Server crashed (exit {self.proc.returncode}); restarting.\n{tail}")
        time.sleep(min(60, 5 * len(self.crashes)))
        try:
            self.proc = self.m.start_server()
        except Exception as e:
            log.error("restart failed: %s", e)
        return True

    def _forward_console(self) -> None:
        """Pass lines typed into this terminal through to the server console."""
        if not sys.stdin or not sys.stdin.isatty():
            return

        def pump():
            for line in sys.stdin:
                if self.proc and self.proc.running and line.strip():
                    self.proc.send(line.rstrip("\n"))
        threading.Thread(target=pump, daemon=True, name="console").start()

    # ------------------------------------------------------------- updates
    def check_for_updates(self, force: bool = False, target: str | None = None) -> None:
        cfg = self.m.config.updates
        self.next_check = time.monotonic() + cfg.check_interval
        try:
            decision, changes = self.m.check(target)
        except Exception as e:
            log.warning("update check failed: %s", e)
            return
        for plan in decision.blocked:
            if plan.minecraft == decision.latest and plan.fingerprint not in self.announced:
                self.announced.add(plan.fingerprint)
                waiting = ", ".join(b.name for b in plan.blockers) or f"{plan.loader} loader"
                self.m.notifier.send(f"Minecraft {plan.minecraft} is out; waiting on: {waiting}")
        if not decision.plan or not changes or changes.empty:
            return
        summary = "\n".join(changes.summary())
        if not (cfg.auto_upgrade or force):
            if decision.plan.fingerprint not in self.announced:
                self.announced.add(decision.plan.fingerprint)
                self.m.notifier.send(f"Update available (run `mcsm update` to apply):\n{summary}")
            return
        missing = self.m.missing_manual(decision.plan)
        if missing:
            key = "manual:" + decision.plan.fingerprint
            if key not in self.announced:
                self.announced.add(key)
                self.m.notifier.send(str(ManualDownloadRequired(missing, self.m.config.manual_dir)))
            return
        if cfg.wait_for_empty and self.proc and self.proc.running and not force:
            online = self.proc.players_online()
            if online:
                log.info("update ready but %s player(s) online; waiting", online)
                self.next_check = time.monotonic() + EMPTY_RETRY
                return
        result = self.m.apply(decision.plan, server=self.proc, restart=True)
        log.info(result.message)
        if result.process:
            self.proc = result.process
        elif self.proc and not self.proc.running:
            # Neither the new nor the old version came back up; let crash handling retry.
            self.proc.stopping = False
