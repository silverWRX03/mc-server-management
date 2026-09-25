"""The web UI: a small JSON API plus a static single-page app, served from ``mcsm run --web``.

Security model: listen on localhost by default; every API call (except login)
needs a session cookie obtained with the password; the cookie is HttpOnly and
SameSite=Strict, and state-changing requests must also carry an ``X-MCSM``
header, which cross-site pages cannot add without a CORS preflight we never allow.
Put it behind an HTTPS reverse proxy before exposing it beyond your machine.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import shutil
import tempfile
import threading
import time
import tomllib
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from . import __version__, backup, config as configmod
from .config import ConfigError, ModSpec
from .daemon import Daemon
from .http import sha1_file
from .java import JavaError
from .mods import ModError
from .mods.modrinth import ModrinthProvider
from .players import PlayerError, Players
from .properties import read_properties

log = logging.getLogger(__name__)

SESSION_COOKIE = "mcsm_session"
SESSION_TTL = 7 * 86400
MAX_JSON = 1 << 20
MAX_UPLOAD = 512 << 20
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def load_password(daemon: Daemon) -> tuple[str, bool]:
    """The configured password, or a generated one kept in .mcsm/web-password."""
    cfg = daemon.m.config
    if cfg.web.password:
        return cfg.web.password, False
    path = cfg.state_dir / "web-password"
    if path.exists():
        return path.read_text().strip(), True
    password = secrets.token_urlsafe(12)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(password + "\n")
    path.chmod(0o600)
    return password, True


class WebUI:
    def __init__(self, daemon: Daemon, host: str | None = None, port: int | None = None):
        self.d = daemon
        cfg = daemon.m.config.web
        self.host = host or cfg.host
        self.port = cfg.port if port is None else port
        self.password, generated = load_password(daemon)
        self.generated = generated
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None
        self.api = Api(self)

    @property
    def url(self) -> str:
        host = "localhost" if self.host in ("127.0.0.1", "0.0.0.0", "::") else self.host
        return f"http://{host}:{self.httpd.server_address[1] if self.httpd else self.port}/"

    def start(self) -> None:
        ui = self

        class Handler(RequestHandler):
            web = ui
        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="web").start()
        where = f"password in {self.d.m.config.state_dir / 'web-password'}" if self.generated \
            else "password from [web] in mcsm.toml"
        log.info("web UI at %s (%s)", self.url, where)

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    # ------------------------------------------------------------ sessions
    def login(self, password: str, client: str) -> str:
        now = time.time()
        with self.lock:
            recent = [t for t in self.failures.get(client, []) if now - t < 300]
            if len(recent) >= 5:
                raise ApiError(429, "too many attempts; wait a few minutes")
            if not hmac.compare_digest(password.encode(), self.password.encode()):
                self.failures[client] = recent + [now]
                raise ApiError(401, "wrong password")
            self.failures.pop(client, None)
            token = secrets.token_urlsafe(32)
            self.sessions = {t: exp for t, exp in self.sessions.items() if exp > now}
            self.sessions[token] = now + SESSION_TTL
            return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self.lock:
            exp = self.sessions.get(token)
            return exp is not None and exp > time.time()

    def logout(self, token: str | None) -> None:
        with self.lock:
            self.sessions.pop(token or "", None)


class RequestHandler(BaseHTTPRequestHandler):
    web: WebUI
    server_version = f"mcsm/{__version__}"

    def log_message(self, fmt, *args):  # keep the server console clean
        log.debug("web: " + fmt, *args)

    # --------------------------------------------------------------- utils
    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any, headers: dict[str, str] | None = None):
        self._send(status, json.dumps(obj).encode(), "application/json", headers)

    def _token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else None

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "invalid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "expected a JSON object")
        return data

    # ------------------------------------------------------------ routing
    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path in STATIC:
            name, ctype = STATIC[path]
            body = resources.files("mcsm").joinpath("webui", name).read_bytes()
            return self._send(200, body, ctype)
        self._dispatch("GET", path, urllib.parse.parse_qs(qs))

    def do_POST(self):
        path, _, qs = self.path.partition("?")
        self._dispatch("POST", path, urllib.parse.parse_qs(qs))

    def _dispatch(self, method: str, path: str, query: dict[str, list[str]]):
        try:
            if not path.startswith("/api/"):
                raise ApiError(404, "not found")
            if method == "POST" and self.headers.get("X-MCSM") != "1":
                raise ApiError(403, "missing X-MCSM header")
            if path == "/api/login" and method == "POST":
                token = self.web.login(str(self._body().get("password", "")), self.client_address[0])
                cookie = (f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; "
                          f"Max-Age={SESSION_TTL}")
                return self._json(200, {"ok": True}, {"Set-Cookie": cookie})
            if not self.web.valid(self._token()):
                raise ApiError(401, "login required")
            if path == "/api/logout":
                self.web.logout(self._token())
                return self._json(200, {"ok": True},
                                  {"Set-Cookie": f"{SESSION_COOKIE}=; Max-Age=0; Path=/; SameSite=Strict"})
            handler = self.web.api.routes.get((method, path))
            if handler is None:
                raise ApiError(404, "not found")
            q = {k: v[-1] for k, v in query.items()}
            if path == "/api/manual/upload":
                return self._json(200, self.web.api.upload(self, q))
            body = self._body() if method == "POST" else {}
            return self._json(200, handler(q, body))
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except (ConfigError, ModError, JavaError, PlayerError, RuntimeError, ValueError, OSError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # pragma: no cover - last resort
            log.exception("web request failed")
            self._json(500, {"error": f"internal error: {e}"})


class Api:
    def __init__(self, web: WebUI):
        self.web = web
        self.d: Daemon = web.d
        r: dict[tuple[str, str], Callable[[dict, dict], Any]] = {}
        get = lambda p, f: r.__setitem__(("GET", p), f)    # noqa: E731
        post = lambda p, f: r.__setitem__(("POST", p), f)  # noqa: E731
        get("/api/status", self.status)
        get("/api/console", self.console)
        get("/api/events", self.events)
        post("/api/command", self.command)
        post("/api/server/start", lambda q, b: self._job("start", self.d.start_server))
        post("/api/server/stop", lambda q, b: self._job("stop", self.d.stop_server))
        post("/api/server/restart", lambda q, b: self._job("restart", self.d.restart_server))
        get("/api/updates", lambda q, b: {"check": self.d.last_check})
        post("/api/updates/check", lambda q, b: self._job("update check", self.d.check_only, b.get("target")))
        post("/api/updates/apply", lambda q, b: self._job(
            "update", self.d.check_for_updates, True, b.get("target") or None))
        get("/api/mods", self.mods)
        get("/api/mods/search", self.search)
        post("/api/mods/add", self.add_mod)
        post("/api/mods/remove", self.remove_mod)
        post("/api/mods/required", self.set_required)
        post("/api/manual/upload", lambda q, b: None)  # handled specially (raw body)
        get("/api/backups", self.backups)
        post("/api/backups/create", self.create_backup)
        post("/api/backups/restore", self.restore_backup)
        get("/api/players", self.players)
        post("/api/players/action", self.player_action)
        get("/api/java", self.java)
        post("/api/java/install", self.java_install)
        post("/api/java/use", self.java_use)
        get("/api/settings", self.settings)
        post("/api/settings", self.save_settings)
        self.routes = r

    @property
    def m(self):
        return self.d.m

    def _job(self, name: str, fn, *args) -> dict:
        if not self.d.submit(name, fn, *args):
            raise ApiError(409, f"busy: {self.d.job['name'] if self.d.job else 'another task'} is running")
        return {"ok": True, "job": name}

    # -------------------------------------------------------------- status
    def status(self, q, b) -> dict:
        d, m, lk = self.d, self.m, self.m.lock
        props = read_properties(m.server_dir / "server.properties")
        return {
            "version": __version__,
            "state": d.state,
            "job": d.job,
            "last_job": d.last_job,
            "want_running": d.want_running,
            "uptime": time.time() - d.started_at if d.started_at and d.state == "running" else None,
            "minecraft": lk.minecraft,
            "loader": lk.loader or m.config.server.loader,
            "loader_version": lk.loader_version,
            "java_major": lk.java_major,
            "java_forced": m.config.java_version,
            "mods": len(lk.mods),
            "players": sorted(d.players),
            "max_players": int(props.get("max-players", "20") or 20),
            "motd": props.get("motd", ""),
            "port": props.get("server-port", "25565"),
            "server_dir": str(m.server_dir),
            "strategy": m.config.updates.strategy,
            "auto_upgrade": m.config.updates.auto_upgrade,
            "update": self._update_summary(),
        }

    def _update_summary(self) -> dict | None:
        c = self.d.last_check
        if not c:
            return None
        return {"checked_at": c["checked_at"], "up_to_date": c["up_to_date"], "target": c["target"],
                "latest": c["latest"], "manual": len(c["manual"])}

    def console(self, q, b) -> dict:
        items, last = self.d.console.since(int(q.get("since", 0) or 0), 1000)
        return {"lines": [{"seq": i["seq"], "text": i["text"], "user": i.get("source") == "user"} for i in items],
                "last": last}

    def events(self, q, b) -> dict:
        items, last = self.d.events.since(int(q.get("since", 0) or 0), 200)
        return {"events": items, "last": last}

    def command(self, q, b) -> dict:
        command = str(b.get("command", "")).strip().lstrip("/")
        if not command or "\n" in command:
            raise ApiError(400, "enter a single command")
        self.d.send_command(command)
        return {"ok": True}

    # ---------------------------------------------------------------- mods
    def mods(self, q, b) -> dict:
        lk = self.m.lock
        return {
            "loader": self.m.config.server.loader,
            "installed": [{"key": x.key, "name": x.name, "version": x.version_number, "filename": x.filename,
                           "source": x.source, "dependency_of": x.dependency_of, "manual": x.manual}
                          for x in lk.mods],
            "configured": [{"source": s.source, "id": s.id, "required": s.required} for s in self.m.config.mods],
            "skipped": [{"key": k, "reason": v} for k, v in lk.skipped.items()],
            "unmanaged": self.m.unmanaged_jars(),
        }

    def _modrinth(self) -> ModrinthProvider:
        return self.m.providers.get("modrinth") or ModrinthProvider(self.m.http)

    def search(self, q, b) -> dict:
        query = q.get("q", "").strip()
        if not query:
            return {"results": []}
        results = self._modrinth().search(query, self.m.loader.mod_loaders)
        listed = {s.id for s in self.m.config.mods if s.source == "modrinth"}
        for r in results:
            r["listed"] = r["id"] in listed or r["slug"] in listed
        return {"results": results}

    def add_mod(self, q, b) -> dict:
        source = b.get("source", "modrinth")
        if source not in configmod.MOD_SOURCES:
            raise ApiError(400, "unknown source")
        mod_id = str(b.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", mod_id):
            raise ApiError(400, "invalid mod id")
        project = self.m.providers[source].project(mod_id)
        if project.server_side == "unsupported":
            raise ApiError(400, f"{project.name} is client-side only")
        if any(s.source == source and s.id in (mod_id, project.id, project.slug) for s in self.m.config.mods):
            raise ApiError(409, f"{project.name} is already listed")
        configmod.append_mod(self.m.config.path, ModSpec(source, project.slug or project.id,
                                                         required=bool(b.get("required", True))))
        self.m.reload_config()
        log.info("added %s", project.name)
        return {"ok": True, "name": project.name}

    def remove_mod(self, q, b) -> dict:
        if not configmod.remove_mod(self.m.config.path, b.get("source", "modrinth"), str(b.get("id", ""))):
            raise ApiError(404, "not listed in mcsm.toml")
        self.m.reload_config()
        log.info("removed %s from mcsm.toml", b.get("id"))
        return {"ok": True}

    def set_required(self, q, b) -> dict:
        source, mod_id = b.get("source", "modrinth"), str(b.get("id", ""))
        if not configmod.remove_mod(self.m.config.path, source, mod_id):
            raise ApiError(404, "not listed in mcsm.toml")
        configmod.append_mod(self.m.config.path, ModSpec(source, mod_id, required=bool(b.get("required"))))
        self.m.reload_config()
        return {"ok": True}

    def upload(self, handler: RequestHandler, q: dict) -> dict:
        """Receive a manually downloaded mod (for authors who block third-party downloads)."""
        if handler.headers.get("X-MCSM") != "1":
            raise ApiError(403, "missing X-MCSM header")
        filename = q.get("filename", "")
        expected = {x["filename"]: x for x in (self.d.last_check or {}).get("manual", [])}
        if filename not in expected or Path(filename).name != filename:
            raise ApiError(400, "that file isn't one of the manual downloads mcsm is waiting for")
        length = int(handler.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            raise ApiError(413, "file is empty or too large")
        folder = self.m.config.manual_dir
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".upload-")
        try:
            with open(fd, "wb") as out:
                remaining = length
                while remaining:
                    chunk = handler.rfile.read(min(1 << 16, remaining))
                    if not chunk:
                        raise ApiError(400, "upload interrupted")
                    out.write(chunk)
                    remaining -= len(chunk)
            decision_mod = next((x for x in self._planned_mods() if x.filename == filename), None)
            if decision_mod and decision_mod.sha1 and sha1_file(Path(tmp)) != decision_mod.sha1.lower():
                raise ApiError(400, "that file doesn't match the one CurseForge lists (checksum differs); "
                                    "make sure you downloaded the exact file from the link")
            shutil.move(tmp, folder / filename)
        finally:
            Path(tmp).unlink(missing_ok=True)
        log.info("received manual download %s", filename)
        c = self.d.last_check
        if c:
            c["manual"] = [x for x in c["manual"] if x["filename"] != filename]
        return {"ok": True}

    def _planned_mods(self):
        target = (self.d.last_check or {}).get("target")
        if not target:
            return []
        return self.m.planner().plan_for(target).mods

    # ------------------------------------------------------------- players
    def _players(self) -> Players:
        running = self.d.state == "running"
        return Players(self.m.server_dir, self.m.http, self.d.send_command if running else None)

    def players(self, q, b) -> dict:
        return self._players().summary(self.d.players)

    def player_action(self, q, b) -> dict:
        if self.d.state == "starting" or (self.d.state == "stopped" and self.d.job):
            # Editing the JSON files now could race with the server starting up.
            raise ApiError(409, "the server is starting; try again in a moment")
        action = str(b.get("action", ""))
        message = self._players().act(action, str(b.get("name", "")), b.get("reason"))
        log.info("players: %s", message)
        return {"ok": True, "message": message}

    # ------------------------------------------------------------- backups
    def backups(self, q, b) -> dict:
        return {"backups": [{"name": p.name, "size": p.stat().st_size, "time": p.stat().st_mtime}
                            for p in reversed(backup.list_backups(self.m.config.backups.dir))]}

    def create_backup(self, q, b) -> dict:
        label = re.sub(r"[^A-Za-z0-9_-]", "_", str(b.get("label") or "manual"))[:40]

        def run():
            if self.d.proc and self.d.proc.running:
                self.d.proc.send("save-off")
                self.d.proc.send("save-all flush")
                time.sleep(5)
            try:
                path = backup.create(self.m.server_dir, self.m.config.backups.dir, label,
                                     self.m.config.backups.exclude)
            finally:
                if self.d.proc and self.d.proc.running:
                    self.d.proc.send("save-on")
            backup.prune(self.m.config.backups.dir, self.m.config.backups.keep)
            return f"created {path.name}"
        return self._job("backup", run)

    def restore_backup(self, q, b) -> dict:
        name = str(b.get("name", ""))
        path = self.m.config.backups.dir / name
        if Path(name).name != name or not path.is_file():
            raise ApiError(404, "no such backup")
        if self.d.state != "stopped":
            raise ApiError(409, "stop the server before restoring a backup")

        def run():
            backup.restore(path, self.m.server_dir)
            return f"restored {name}; run an update check to re-sync mods"
        return self._job("restore", run)

    # ----------------------------------------------------------------- java
    def java(self, q, b) -> dict:
        jm, lk = self.m.java, self.m.lock
        current = None
        if lk.java_major:
            try:
                current = jm.select(lk.java_major, install=False)
            except JavaError as e:
                current = f"(none: {e})"
        return {
            "required": lk.java_major,
            "forced": self.m.config.java_version,
            "auto_install": self.m.config.java_auto_install,
            "current": current,
            "managed": [{"major": j.major, "release": j.release, "path": str(j.binary)}
                        for j in jm.installed().values()],
            "configured": [{"major": k, "path": v} for k, v in sorted(self.m.config.java_versions.items())],
            "default": self.m.config.java_default,
        }

    def java_install(self, q, b) -> dict:
        major = int(b.get("major", 0))
        if not 8 <= major <= 99:
            raise ApiError(400, "invalid Java version")
        return self._job(f"install Java {major}", lambda: f"installed {self.m.java.install(major).release}")

    def java_use(self, q, b) -> dict:
        value = str(b.get("version", "auto"))
        if value != "auto":
            if not value.isdigit():
                raise ApiError(400, 'use a major version such as 21, or "auto"')
            if self.m.lock.java_major and int(value) < self.m.lock.java_major:
                raise ApiError(400, f"Minecraft {self.m.lock.minecraft} needs Java {self.m.lock.java_major}+")
        configmod.set_value(self.m.config.path, "java", "version", json.dumps(value) if value == "auto" else value)
        self.m.reload_config()
        return {"ok": True, "note": "applies the next time the server starts"}

    # ------------------------------------------------------------- settings
    SETTINGS = {
        # key: (table, toml key, type)
        "memory": ("server", "memory", str),
        "restart_on_crash": ("server", "restart_on_crash", bool),
        "strategy": ("updates", "strategy", str),
        "mod_channel": ("updates", "mod_channel", str),
        "auto_upgrade": ("updates", "auto_upgrade", bool),
        "check_interval": ("updates", "check_interval", str),
        "warn_minutes": ("updates", "warn_minutes", list),
        "wait_for_empty": ("updates", "wait_for_empty", bool),
        "verify_boot": ("updates", "verify_boot", bool),
        "backups_keep": ("backups", "keep", int),
        "discord_webhook": ("notify", "discord_webhook", str),
    }

    def settings(self, q, b) -> dict:
        c = self.m.config
        interval = c.updates.check_interval
        return {
            "memory": c.server.memory, "restart_on_crash": c.restart_on_crash,
            "strategy": c.updates.strategy, "mod_channel": c.updates.mod_channel,
            "auto_upgrade": c.updates.auto_upgrade,
            "check_interval": f"{interval // 3600}h" if interval % 3600 == 0 else f"{interval // 60}m",
            "warn_minutes": c.updates.warn_minutes, "wait_for_empty": c.updates.wait_for_empty,
            "verify_boot": c.updates.verify_boot, "backups_keep": c.backups.keep,
            "discord_webhook": c.discord_webhook,
            "choices": {"strategy": configmod.STRATEGIES, "mod_channel": configmod.CHANNELS},
        }

    def save_settings(self, q, b) -> dict:
        path = self.m.config.path
        original = path.read_text()
        try:
            for key, value in b.items():
                if key not in self.SETTINGS:
                    raise ApiError(400, f"unknown setting {key}")
                table, toml_key, kind = self.SETTINGS[key]
                if kind is bool:
                    literal = "true" if value is True else "false" if value is False else None
                elif kind is int:
                    literal = str(int(value))
                elif kind is list:
                    literal = json.dumps([int(x) for x in value])
                else:
                    literal = json.dumps(str(value))
                if literal is None:
                    raise ApiError(400, f"{key} must be true or false")
                configmod.set_value(path, table, toml_key, literal)
            configmod.parse(self.m.config.root, tomllib.loads(path.read_text()))  # validate
            self.m.reload_config()
        except Exception:
            path.write_text(original)
            raise
        log.info("settings saved")
        return {"ok": True}
