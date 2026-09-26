"""Running the Minecraft server as a child process."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .desktop import NO_WINDOW

log = logging.getLogger(__name__)

READY = re.compile(r"\]: Done \([\d.,]+s\)!|^Done \([\d.,]+s\)!")
PLAYERS = re.compile(r"There are (\d+) (?:of a max of|/) ?(\d+) players online")
# Anchored right after the logger prefix so chat ("<Steve> Bob joined the game") can't spoof it.
JOINED = re.compile(r"\]: ([A-Za-z0-9_]{1,16}) joined the game$")
LEFT = re.compile(r"\]: ([A-Za-z0-9_]{1,16}) left the game$")


class ServerProcess:
    def __init__(self, argv: list[str], cwd: Path, echo: bool = True,
                 on_line: Callable[[str], None] | None = None):
        self.argv = argv
        self.cwd = cwd
        self.echo = echo
        self.on_line = on_line
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=500)
        self.line_count = 0
        self.ready = threading.Event()
        self._line_cond = threading.Condition()
        self._reader: threading.Thread | None = None
        self.stopping = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def start(self) -> None:
        if self.running:
            raise RuntimeError("server is already running")
        log.info("starting server: %s", " ".join(self.argv))
        self.ready.clear()
        self.stopping = False
        self.lines.clear()
        self.proc = subprocess.Popen(
            self.argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace", **NO_WINDOW)
        self._reader = threading.Thread(target=self._read, daemon=True, name="server-output")
        self._reader.start()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.rstrip("\r\n")
            if self.echo:
                print(line, flush=True)
            with self._line_cond:
                self.lines.append(line)
                self.line_count += 1
                self._line_cond.notify_all()
            if READY.search(line):
                self.ready.set()
            if self.on_line:
                try:
                    self.on_line(line)
                except Exception:  # never let a callback kill the reader
                    log.exception("output callback failed")
        with self._line_cond:
            self._line_cond.notify_all()

    def wait_ready(self, timeout: float) -> bool:
        """Wait until the server logs ``Done (...)!``. False on timeout or if it exits."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ready.wait(timeout=1):
                return True
            if not self.running:
                return False
        return False

    def send(self, command: str) -> None:
        if not self.running or not self.proc or not self.proc.stdin:
            raise RuntimeError("server is not running")
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def say(self, message: str) -> None:
        if self.running:
            self.send(f"say {message}")

    def players_online(self, timeout: float = 5) -> int | None:
        """Ask the server how many players are online (None if it doesn't answer)."""
        if not self.running:
            return None
        with self._line_cond:
            start = self.line_count
            self.send("list")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                new = min(self.line_count - start, len(self.lines))
                for line in list(self.lines)[len(self.lines) - new:]:
                    if m := PLAYERS.search(line):
                        return int(m.group(1))
                self._line_cond.wait(timeout=0.5)
        return None

    def stop(self, timeout: float = 120) -> int | None:
        """Stop gracefully with ``stop``; terminate, then kill, if it hangs."""
        if not self.proc:
            return None
        self.stopping = True
        if self.running:
            try:
                self.send("stop")
            except (RuntimeError, BrokenPipeError, OSError):
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("server did not stop within %ss; terminating", timeout)
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        if self._reader:
            self._reader.join(timeout=5)
        return self.proc.returncode

    def tail(self, n: int = 30) -> list[str]:
        return list(self.lines)[-n:]
