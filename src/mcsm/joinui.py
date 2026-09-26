"""The friend's side of an invite, as a page in their browser ("mcfui").

Double-clicking a server's download opens this page: it shows the server (name,
Minecraft version, mods), lets the friend tick the launchers to add it to (Minecraft
Launcher, Prism Launcher, Modrinth App, CurseForge), and shows progress. It only
listens on 127.0.0.1, behind a random secret in every URL, and stops when the page
is closed. The console flow (join.run_interactive) remains for when no browser opens.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import launchers
from .http import HttpError
from .join import Invite, Joiner, JoinError, _supports_quick_play, validate_pack

log = logging.getLogger(__name__)

HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                               "frame-ancestors 'none'; form-action 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
STATIC = {"": ("join.html", "text/html; charset=utf-8"), "join.js": ("join.js", "text/javascript; charset=utf-8"),
          "style.css": ("style.css", "text/css; charset=utf-8"), "icon.png": ("icon.png", "image/png")}
IDLE_SECONDS = 180  # the page pings while it's open; stop a while after it's closed


class JoinUI:
    def __init__(self, invite: Invite | None, pack: dict | None = None, mc_dir: Path | None = None,
                 prism_dir: Path | None = None, out_dir: Path | None = None, http=None):
        self.invite = invite
        self.pack = validate_pack(pack) if pack else None
        self.pack_error = ""
        self.prism_dir = prism_dir
        self.out_dir = out_dir
        self.lines: list[str] = []
        self.joiner = Joiner(invite or Invite("localhost", 1, "local-" + "0" * 16), mc_dir=mc_dir, http=http,
                             say=self._say)
        self.token = secrets.token_urlsafe(18)
        self.results: list[dict] = []
        self.running = False
        self.done = threading.Event()
        self.last_seen = time.monotonic()
        self._lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------ state
    def _say(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)
        print(line, flush=True)

    def load_pack(self) -> None:
        if self.pack is not None or self.invite is None:
            return
        try:
            self.pack = self.joiner.fetch_pack()
            self.pack_error = ""
        except JoinError as e:
            self.pack_error = str(e)

    def info(self) -> dict:
        self.load_pack()
        p = self.pack
        return {
            "pack": None if p is None else {
                "name": p["name"], "address": p["address"], "minecraft": p["minecraft"], "loader": p["loader"],
                "loader_version": p.get("loader_version"), "mods": [m["name"] for m in p.get("mods", [])],
                "manual": p.get("manual", []), "icon": p.get("icon") if str(p.get("icon") or "").startswith("data:image/png;base64,") else None,
                "quick_play": _supports_quick_play(p["minecraft"])},
            "error": self.pack_error,
            "launchers": [f.to_dict() for f in launchers.detect(self.joiner.mc, self.prism_dir)],
        }

    def setup(self, targets: list[str]) -> None:
        targets = [t for t in targets if t in launchers.KEYS]
        if not targets:
            raise ValueError("pick at least one launcher")
        if self.pack is None:
            raise ValueError(self.pack_error or "the server's details haven't loaded")
        with self._lock:
            if self.running:
                raise RuntimeError("already setting things up")
            self.running = True
            self.results = []

        def work():
            try:
                self._say(f"Setting up Minecraft {self.pack['minecraft']} for {self.pack['name']}")
                self.results = self.joiner.run_targets(self.pack, targets, prism_dir=self.prism_dir, out_dir=self.out_dir)
            except HttpError as e:
                self._say(f"A download failed: {e}. Check your internet connection and try again.")
            except Exception as e:  # keep the page informed whatever happens
                log.exception("setting up failed")
                self._say(f"Something went wrong: {e}")
            finally:
                self.running = False
                self._say("Finished.")
        threading.Thread(target=work, daemon=True).start()

    def open_again(self, key: str) -> bool:
        from . import opener
        r = next((x for x in self.results if x.get("launcher") == key and x.get("ok")), None)
        if r is None:
            return False
        if key == "minecraft":
            return self.joiner.open_launcher()
        if key == "prism":
            return launchers.open_prism(launchers_slug(self.pack), self.pack["address"])
        if key == "modrinth":
            return opener.open_path(Path(r["where"]))
        return opener.reveal(Path(r["where"]))

    # ------------------------------------------------------------ serving
    def start(self) -> str:
        ui = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status: int, body: bytes, ctype: str) -> None:
                self.send_response(status)
                for k, v in HEADERS.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data) -> None:
                self._send(status, json.dumps(data).encode(), "application/json")

            def _route(self) -> str | None:
                host = (self.headers.get("Host") or "").lower()
                port = ui.httpd.server_address[1]
                if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    self._json(403, {"error": "forbidden"})
                    return None
                path = urlparse(self.path).path
                prefix = f"/{ui.token}/"
                if not path.startswith(prefix):
                    self._json(404, {"error": "not found"})
                    return None
                ui.last_seen = time.monotonic()
                return path[len(prefix):]

            def do_GET(self):
                rest = self._route()
                if rest is None:
                    return
                if rest in STATIC:
                    name, ctype = STATIC[rest]
                    self._send(200, resources.files("mcsm").joinpath("webui", name).read_bytes(), ctype)
                elif rest == "api/info":
                    self._json(200, ui.info())
                elif rest == "api/progress":
                    since = int((parse_qs(urlparse(self.path).query).get("since") or ["0"])[0] or 0)
                    with ui._lock:
                        lines = ui.lines[since:]
                        n = len(ui.lines)
                    self._json(200, {"lines": lines, "next": n, "running": ui.running, "results": ui.results})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                rest = self._route()
                if rest is None:
                    return
                if self.headers.get("X-MCSM") != "1":
                    self._json(403, {"error": "missing header"})
                    return
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if rest == "api/setup":
                        ui.setup([str(x) for x in body.get("launchers", [])])
                        self._json(200, {"ok": True})
                    elif rest == "api/open":
                        self._json(200, {"ok": ui.open_again(str(body.get("launcher", "")))})
                    elif rest == "api/quit":
                        self._json(200, {"ok": True})
                        ui.done.set()
                    else:
                        self._json(404, {"error": "not found"})
                except (ValueError, RuntimeError) as e:
                    self._json(400, {"error": str(e)})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{self.token}/"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def wait(self) -> None:
        while not self.done.wait(2):
            if not self.running and time.monotonic() - self.last_seen > IDLE_SECONDS:
                break


def launchers_slug(pack: dict) -> str:
    from .join import slugify
    return slugify(pack["name"])


def run(invite: Invite | None, pack: dict | None = None, mc_dir: Path | None = None,
        open_browser=webbrowser.open) -> int | None:
    """Show the page; ``None`` when no browser could be opened (use the console instead)."""
    ui = JoinUI(invite, pack=pack, mc_dir=mc_dir)
    url = ui.start()
    try:
        if not open_browser(url):
            return None
        print("mcsm opened a page in your browser to set up Minecraft for this server.")
        print(f"If it didn't appear, open {url}")
        print("Keep this window open until you're done there.", flush=True)
        ui.wait()
        return 0 if any(r.get("ok") for r in ui.results) else 1
    finally:
        ui.stop()
