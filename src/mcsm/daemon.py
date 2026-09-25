"""``mcsm run``: keep the server up and keep it updated.

The daemon owns the server process. It restarts it after crashes, checks for
updates on a schedule (or when ``mcsm update`` drops a request file), and applies
them with an in-game countdown. Anything slow (starting, updating, backups, Java
installs) runs as a *job*: one at a time, in the background, so the optional web
UI stays responsive while it happens.
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
from typing import Any, Callable

from . import notice, selfupdate, setup as setupmod
from .manager import Manager, ManualDownloadRequired
from .process import JOINED, LEFT, READY, ServerProcess

log = logging.getLogger(__name__)

CRASH_WINDOW = 600      # seconds
MAX_CRASHES = 3         # within CRASH_WINDOW before giving up
EMPTY_RETRY = 300       # seconds between "is anyone online?" checks when waiting for an empty server
SELF_CHECK_INTERVAL = 24 * 3600


def pid_path(manager: Manager) -> Path:
    return manager.config.state_dir / "daemon.pid"


def request_path(manager: Manager) -> Path:
    return manager.config.state_dir / "update-requested"


def self_update_request_path(manager: Manager) -> Path:
    return manager.config.state_dir / "self-update-requested"


def stop_request_path(manager: Manager) -> Path:
    return manager.config.state_dir / "stop-requested"


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) would send Ctrl+C on Windows, so ask the kernel instead.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, owned by someone else
        return True


def running_pid(manager: Manager) -> int | None:
    try:
        pid = int(pid_path(manager).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def request_stop(manager: Manager) -> None:
    """Ask a running daemon to stop the server cleanly and exit (works on every OS)."""
    path = stop_request_path(manager)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop")


class LogBuffer:
    """A bounded, thread-safe list of entries with increasing sequence numbers."""

    def __init__(self, maxlen: int):
        self._items: deque[dict] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def append(self, **item: Any) -> None:
        with self._lock:
            self._seq += 1
            self._items.append({"seq": self._seq, "time": time.time(), **item})

    def since(self, seq: int, limit: int = 1000) -> tuple[list[dict], int]:
        with self._lock:
            items = [i for i in self._items if i["seq"] > seq]
            return items[-limit:], self._seq


class _EventHandler(logging.Handler):
    def __init__(self, buffer: LogBuffer):
        super().__init__(logging.INFO)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(level=record.levelname.lower(), message=record.getMessage())
        except Exception:
            pass


def decision_to_dict(m: Manager, decision, changes) -> dict:
    plan = decision.plan
    return {
        "checked_at": time.time(),
        "installed": m.lock.minecraft,
        "latest": decision.latest,
        "target": plan.minecraft if plan else None,
        "loader_version": plan.loader_version if plan else None,
        "up_to_date": bool(plan and (changes is None or changes.empty)),
        "changes": changes.summary() if changes else [],
        "dropped": [{"name": b.name, "reason": b.reason} for b in plan.dropped] if plan else [],
        "manual": [{"name": x.name, "filename": x.filename, "url": x.manual_url}
                   for x in m.missing_manual(plan)] if plan else [],
        "blocked": [{
            "minecraft": p.minecraft,
            "loader_missing": p.loader_version is None,
            "blockers": [{"name": b.name, "reason": b.reason} for b in p.blockers],
        } for p in decision.blocked],
    }


class Daemon:
    def __init__(self, manager: Manager, tick: float = 2.0):
        self.m = manager
        self.tick = tick
        self.proc: ServerProcess | None = None
        self.stop_requested = threading.Event()
        self.exit_code = 0
        self.crashes: deque[float] = deque()
        self.next_check = 0.0
        self.announced: set[str] = set()

        self.want_running = True        # False after a deliberate stop (no crash restarts)
        self.web_enabled = False
        self.ui = None
        self.open_browser = False
        self.console = LogBuffer(3000)
        self.events = LogBuffer(500)
        self.players: set[str] = set()
        self.started_at: float | None = None
        self.last_check: dict | None = None
        self.ops = threading.Lock()     # one job at a time
        self.job: dict | None = None
        self.last_job: dict | None = None
        self.self_update: dict | None = None     # a newer mcsm release, if any
        self.next_self_check = time.monotonic() + 30
        self.restart_requested = False           # re-exec mcsm after exiting (self-update)
        manager.on_line = self._on_line
        manager.on_process = self._on_process

    # ------------------------------------------------------------ state
    @property
    def state(self) -> str:
        if self.proc and self.proc.running:
            return "running" if self.proc.ready.is_set() else "starting"
        return "stopped"

    def _on_process(self, proc: ServerProcess) -> None:
        self.proc = proc
        self.players.clear()
        self.started_at = None

    def _on_line(self, line: str) -> None:
        self.console.append(text=line)
        if m := JOINED.search(line):
            self.players.add(m.group(1))
        elif m := LEFT.search(line):
            self.players.discard(m.group(1))
        elif READY.search(line):
            self.started_at = time.time()

    def send_command(self, command: str) -> None:
        if not self.proc or not self.proc.running:
            raise RuntimeError("the server is not running")
        self.console.append(text=f"> {command}", source="user")
        self.proc.send(command)

    # ------------------------------------------------------------- jobs
    def submit(self, name: str, fn: Callable[..., Any], *args: Any) -> bool:
        """Run ``fn`` in the background unless another job is running."""
        if not self.ops.acquire(blocking=False):
            return False
        self.job = {"name": name, "started": time.time()}

        def runner():
            ok, message = True, ""
            try:
                message = fn(*args) or "done"
            except Exception as e:
                ok, message = False, str(e)
                log.error("%s failed: %s", name, e)
            finally:
                self.last_job = {"name": name, "ok": ok, "message": message, "finished": time.time()}
                self.job = None
                self.ops.release()
        threading.Thread(target=runner, daemon=True, name=f"job:{name}").start()
        return True

    def start_server(self) -> str:
        self.want_running = True
        if self.proc and self.proc.running:
            return "already running"
        if not self.m.lock.installed:
            log.info("no server installed yet; installing")
            self.check_for_updates(force=True)
            if not self.m.lock.installed:
                raise RuntimeError("no server could be installed yet; see the Updates page")
            if self.proc and self.proc.running:  # the install left it running
                return "installed and started"
        self.m.start_server()
        self.m.notifier.send(f"Server is up (Minecraft {self.m.lock.minecraft})")
        return "started"

    def stop_server(self) -> str:
        self.want_running = False
        if not self.proc or not self.proc.running:
            return "already stopped"
        self.proc.stop(self.m.config.server.stop_timeout)
        log.info("server stopped")
        return "stopped"

    def restart_server(self) -> str:
        self.stop_server()
        return self.start_server()

    # -------------------------------------------------------- lifecycle
    def run(self, web: bool = False) -> int:
        state = self.m.config.state_dir
        state.mkdir(parents=True, exist_ok=True)
        if (pid := running_pid(self.m)) and pid != os.getpid():
            log.error("mcsm is already running (pid %s)", pid)
            return 1
        pid_path(self.m).write_text(str(os.getpid()))
        stop_request_path(self.m).unlink(missing_ok=True)
        handler = _EventHandler(self.events)
        mcsm_log = logging.getLogger("mcsm")
        mcsm_log.addHandler(handler)
        if mcsm_log.getEffectiveLevel() > logging.INFO:
            mcsm_log.setLevel(logging.INFO)  # the activity feed needs INFO records
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop_requested.set())
        ui = None
        try:
            if web:
                from .web import WebUI
                ui = WebUI(self)
                ui.start()
                self.ui = ui
                self.web_enabled = True
                if self.open_browser:
                    import webbrowser
                    threading.Timer(1.0, webbrowser.open, args=(ui.url,)).start()
            return self._loop()
        finally:
            if ui:
                ui.stop()
            if self.proc and self.proc.running:
                log.info("stopping server")
                self.proc.stop(self.m.config.server.stop_timeout)
            logging.getLogger("mcsm").removeHandler(handler)
            pid_path(self.m).unlink(missing_ok=True)

    def _boot(self) -> str:
        try:
            return self.start_server()
        except Exception:
            self.want_running = False
            if not self.web_enabled:
                # Without the web UI there's nobody to fix things interactively.
                self.exit_code = 1
                self.stop_requested.set()
            raise

    def _loop(self) -> int:
        self._forward_console()
        if not notice.accepted(self.m.config.root):
            log.info("waiting for the first-run notice to be accepted in the web UI")
            while not notice.accepted(self.m.config.root):
                if self.stop_requested.wait(1) or stop_request_path(self.m).exists():
                    stop_request_path(self.m).unlink(missing_ok=True)
                    return self.exit_code
            log.info("notice accepted")
        if self.setup_pending:
            log.info("waiting for the server to be set up in the web UI")
        else:
            self.submit("start", self._boot)
        # Let the first start finish before the first scheduled update check.
        self.next_check = time.monotonic() + 60

        while not self.stop_requested.is_set():
            if stop_request_path(self.m).exists():
                stop_request_path(self.m).unlink(missing_ok=True)
                log.info("stop requested")
                self.stop_requested.set()
                break
            idle = not self.ops.locked()
            if (idle and self.want_running and self.proc is not None and not self.proc.running
                    and not self.proc.stopping):
                self._handle_crash()
            req = request_path(self.m)
            requested = req.exists()
            if self.setup_pending:  # nothing to check or restart until the server exists
                self.stop_requested.wait(self.tick)
                continue
            if idle and (requested or time.monotonic() >= self.next_check):
                target = (req.read_text().strip() or None) if requested else None
                req.unlink(missing_ok=True)
                self.submit("update check", self.check_for_updates, requested, target)
            sreq = self_update_request_path(self.m)
            if idle and sreq.exists():
                sreq.unlink(missing_ok=True)
                self.check_self_update()
                if self.self_update:
                    self.submit(f"update mcsm to {self.self_update['version']}", self.apply_self_update)
            if self.m.config.self_update_check and time.monotonic() >= self.next_self_check:
                self.next_self_check = time.monotonic() + SELF_CHECK_INTERVAL
                threading.Thread(target=self.check_self_update, daemon=True, name="self-update-check").start()
            self.stop_requested.wait(self.tick)
        return self.exit_code

    def _handle_crash(self) -> None:
        now = time.monotonic()
        self.crashes.append(now)
        while self.crashes and now - self.crashes[0] > CRASH_WINDOW:
            self.crashes.popleft()
        tail = "\n".join(self.proc.tail(15))
        self.proc.stopping = True  # handled; don't count this exit twice
        if not self.m.config.restart_on_crash or len(self.crashes) > MAX_CRASHES:
            self.m.notifier.send(f"Server stopped unexpectedly (exit {self.proc.returncode}); not restarting.\n{tail}")
            self.want_running = False
            if not self.web_enabled:
                self.exit_code = 1
                self.stop_requested.set()
            return
        self.m.notifier.send(f"Server crashed (exit {self.proc.returncode}); restarting.\n{tail}")
        delay = min(60, 5 * len(self.crashes))

        def restart():
            self.stop_requested.wait(delay)
            if self.stop_requested.is_set() or not self.want_running:
                return "cancelled"
            try:
                return self.start_server()
            except Exception:
                if self.proc:
                    self.proc.stopping = False  # let crash handling try again
                raise
        self.submit("restart after crash", restart)

    def _forward_console(self) -> None:
        """Pass lines typed into this terminal through to the server console."""
        if not sys.stdin or not sys.stdin.isatty():
            return

        def pump():
            for line in sys.stdin:
                if self.proc and self.proc.running and line.strip():
                    self.proc.send(line.rstrip("\n"))
        threading.Thread(target=pump, daemon=True, name="console").start()

    # ---------------------------------------------------------- updates
    def check_only(self, target: str | None = None) -> str:
        decision, changes = self.m.check(target, retry_failed=True)
        self.last_check = decision_to_dict(self.m, decision, changes)
        if self.last_check["up_to_date"]:
            return f"up to date (Minecraft {self.m.lock.minecraft})"
        if decision.plan:
            return f"update available: Minecraft {decision.plan.minecraft}"
        return "no installable combination found"

    def check_for_updates(self, force: bool = False, target: str | None = None) -> str:
        cfg = self.m.config.updates
        self.next_check = time.monotonic() + cfg.check_interval
        try:
            decision, changes = self.m.check(target, retry_failed=force)
        except Exception as e:
            log.warning("update check failed: %s", e)
            return f"update check failed: {e}"
        self.last_check = decision_to_dict(self.m, decision, changes)
        for plan in decision.blocked:
            if plan.minecraft == decision.latest and plan.fingerprint not in self.announced:
                self.announced.add(plan.fingerprint)
                if any(b.key == "mcsm:failed" for b in plan.blockers):
                    continue  # already reported when it failed
                waiting = ", ".join(b.name for b in plan.blockers) or f"{plan.loader} loader"
                self.m.notifier.send(f"Minecraft {plan.minecraft} is out; waiting on: {waiting}")
        if not decision.plan or not changes or changes.empty:
            return "up to date" if decision.plan else "no installable combination found"
        summary = "\n".join(changes.summary())
        if not (cfg.auto_upgrade or force):
            if decision.plan.fingerprint not in self.announced:
                self.announced.add(decision.plan.fingerprint)
                self.m.notifier.send(f"Update available (run `mcsm update` to apply):\n{summary}")
            return "update available (automatic upgrades are off)"
        missing = self.m.missing_manual(decision.plan)
        if missing:
            key = "manual:" + decision.plan.fingerprint
            if key not in self.announced:
                self.announced.add(key)
                self.m.notifier.send(str(ManualDownloadRequired(missing, self.m.config.manual_dir)))
            return f"waiting for {len(missing)} manual download(s)"
        if cfg.wait_for_empty and self.proc and self.proc.running and not force:
            online = self.proc.players_online()
            if online:
                log.info("update ready but %s player(s) online; waiting", online)
                self.next_check = time.monotonic() + EMPTY_RETRY
                return f"waiting for {online} player(s) to leave"
        was_running = bool(self.proc and self.proc.running)
        result = self.m.apply(decision.plan, server=self.proc, restart=was_running or self.want_running)
        if result.ok:
            log.info(result.message)  # failures are already logged by the rollback
        if result.process:
            self.proc = result.process
        elif self.proc and not self.proc.running and self.want_running:
            # Neither the new nor the old version came back up; let crash handling retry.
            self.proc.stopping = False
        try:
            self.last_check = decision_to_dict(self.m, *self.m.check())
        except Exception:
            pass
        if not result.ok:
            raise RuntimeError(result.message)
        return result.message

    # ------------------------------------------------------------ setup
    @property
    def setup_pending(self) -> bool:
        return setupmod.is_pending(self.m.config.root)

    def run_setup(self, spec: "setupmod.SetupSpec") -> str:
        """First-time setup from the web UI: write the config, then install and boot the server."""
        if self.m.lock.installed:
            raise RuntimeError("this server is already set up")
        setupmod.configure(self.m.config.root, spec)
        self.m.reload_config()
        log.info("setting up a %s server (Minecraft %s, %d mod(s))", spec.loader, spec.minecraft,
                 len(spec.mods) + len(spec.optional_mods))
        self.check_only()
        if not self.last_check or not self.last_check.get("target"):
            blocked = self.last_check.get("blocked", []) if self.last_check else []
            reasons = [f"{b['name']}: {b['reason']}" for p in blocked[:1] for b in p["blockers"]]
            if blocked and blocked[0].get("loader_missing"):
                reasons.insert(0, f"{spec.loader} has no build for Minecraft {blocked[0]['minecraft']} yet")
            raise RuntimeError("no Minecraft version works with these choices"
                               + (": " + "; ".join(reasons) if reasons else ""))
        if self.last_check.get("manual"):
            names = ", ".join(m["name"] for m in self.last_check["manual"])
            raise RuntimeError(f"these mods must be downloaded by hand first (see the Updates page): {names}")
        self.start_server()
        setupmod.clear_pending(self.m.config.root)
        self.next_check = time.monotonic() + self.m.config.updates.check_interval
        return f"your server is ready: Minecraft {self.m.lock.minecraft}"

    # ------------------------------------------------------- self-update
    def check_self_update(self) -> str:
        try:
            release = selfupdate.check(self.m.http)
        except Exception as e:
            log.debug("mcsm update check failed: %s", e)
            return f"couldn't check for mcsm updates: {e}"
        if release is None:
            self.self_update = None
            return f"mcsm {selfupdate.__version__} is the latest version"
        can, why = selfupdate.install_method(release)
        first = self.self_update is None or self.self_update.get("version") != release.version
        self.self_update = {**release.to_dict(), "current": selfupdate.__version__, "can_install": can, "reason": why}
        if first:
            self.m.notifier.send(f"mcsm {release.version} is available (you have {selfupdate.__version__}). "
                                 f"Update from the web UI or with `mcsm self-update`.")
        return f"mcsm {release.version} is available"

    def apply_self_update(self) -> str:
        """Install the new mcsm, stop the server cleanly, and restart mcsm on the new version."""
        info = self.self_update
        if not info:
            raise RuntimeError("no mcsm update is available")
        release = selfupdate.Release(info["version"], info["tag"], info["url"], info["notes"], info.get("assets", {}))
        message = selfupdate.install(release, http=self.m.http)
        if self.proc and self.proc.running:
            if self.players:
                self.proc.say("Server restarting in 1 minute: updating the server manager")
                self.stop_requested.wait(60)
            self.proc.say("Restarting now!")
        self.m.notifier.send(f"{message}; restarting mcsm")
        self.restart_requested = True
        self.stop_requested.set()  # run() stops the server; cli re-executes mcsm
        return message
