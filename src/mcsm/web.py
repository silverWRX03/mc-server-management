"""The web UI: a small JSON API plus a static single-page app, served from ``mcsm run --web``.

Security model: listen on localhost by default; every API call (except login)
needs a session cookie obtained with the password or PIN (see webauth.py); the
cookie is HttpOnly and SameSite=Strict, and state-changing requests must also carry
an ``X-MCSM`` header, which cross-site pages cannot add without a CORS preflight we
never allow. Requests must name this machine in their Host header, so a web page
can't reach the panel through DNS rebinding. "No password" only works for browsers
on this computer. Put it behind an HTTPS reverse proxy before exposing it beyond
your machine.
"""

from __future__ import annotations

import ipaddress
import json
import os
import logging
import re
import secrets
import shutil
import socket
import socketserver
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

from . import __version__, backup, config as configmod, configs, licenses, notice, serverprops, setup as setupmod, stats, webauth
from .config import ConfigError, ModSpec
from .daemon import Daemon, set_current_server
from .hub import Hub
from .minecraft import Mojang
from .http import HttpError, sha1_file
from .java import JavaError
from .mods import ModError
from .mods.modrinth import ModrinthProvider
from .players import PlayerError, Players
from .properties import read_properties, write_properties
from .skins import SkinError, Skins

log = logging.getLogger(__name__)

SESSION_COOKIE = "mcsm_session"
SESSION_TTL = 7 * 86400
MAX_JSON = 1 << 20
MAX_UPLOAD = 512 << 20
MAX_ARCHIVE = 64 << 30
LOCAL_ONLY = {"/api/open", "/api/hub/open"}  # they act on this computer's screen  # a whole server (worlds and all), for importing
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8"),
          "/icon.png": ("icon.png", "image/png")}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' https: data:; style-src 'self'; "
                               "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


# Reachable before the first-run notice has been accepted.
NOTICE_EXEMPT = {"/api/notice", "/api/notice/accept", "/api/status", "/api/licenses", "/api/auth/change", "/api/hub"}
# Routes whose request body is a file, streamed to disk rather than parsed as JSON.
RAW_UPLOADS = {"/api/hub/stage", "/api/mods/local"}
SERVER_PATH = re.compile(r"^/api/servers/([a-z0-9][a-z0-9-]{0,63})(/.*)$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def host_allowed(host_header: str | None, extra: list[str]) -> bool:
    """Whether a request's Host header names this machine (guards against DNS rebinding)."""
    if not host_header:
        return True  # HTTP/1.0 clients; browsers always send one
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host[1:host.find("]")] if "]" in host else host
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host == "localhost" or host.endswith((".localhost", ".local")) or host in extra:
        return True
    name = socket.gethostname().lower()
    return host in (name, name.split(".")[0])


def is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_loopback
    except ValueError:
        return False


class WebUI:
    def __init__(self, hub: Hub, host: str | None = None, port: int | None = None):
        self.hub = hub
        cfg = hub.web
        self.host = host or cfg.host
        self.port = cfg.port if port is None else port
        self.store = webauth.AuthStore(hub)   # the hub has the state_dir and [web] settings
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None
        self._apis: dict[str, Api] = {}
        self.hub_api = HubApi(self)

    @property
    def auth(self) -> webauth.Auth:
        return self.store.get()

    def api_for(self, sid: str) -> Api:
        d = self.hub.get(sid)
        if d is None:
            raise ApiError(404, "there's no server with that id (it may have been removed)")
        api = self._apis.get(sid)
        if api is None or api.d is not d:
            api = self._apis[sid] = Api(self, d, sid)
        return api

    @property
    def api(self) -> Api:
        """The server's API when there is only one (`mcsm run`)."""
        only = self.hub.only()
        if not only:
            raise ApiError(404, "pick a server first")
        return self.api_for(only[0])

    @property
    def url(self) -> str:
        host = "localhost" if self.host in ("127.0.0.1", "0.0.0.0", "::") else self.host
        return f"http://{host}:{self.httpd.server_address[1] if self.httpd else self.port}/"

    def start(self) -> None:
        ui = self

        class Handler(RequestHandler):
            web = ui
        self.httpd = _Server((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="web").start()
        log.info("web UI at %s - password: %s", self.url, webauth.describe(self.auth))

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    # ------------------------------------------------------------ sessions
    def login(self, password: str, client: str) -> str:
        now = time.time()
        auth = self.auth
        with self.lock:
            recent = [t for t in self.failures.get(client, []) if now - t < 300]
            if len(recent) >= 5:
                raise ApiError(429, "too many attempts; wait a few minutes")
        if auth.mode == "none" or not auth.check(password):  # "none" never needs (or accepts) a login
            with self.lock:
                self.failures[client] = recent + [now]
            raise ApiError(401, "wrong PIN" if auth.mode == "pin" else "wrong password")
        with self.lock:
            self.failures.pop(client, None)
            return self._new_session(now)

    def _new_session(self, now: float) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions = {t: exp for t, exp in self.sessions.items() if exp > now}
        self.sessions[token] = now + SESSION_TTL
        return token

    def change(self, mode: str, secret: str, local: bool) -> str:
        """Change how the panel is protected; signs out everyone else and returns a new session."""
        if mode == "none" and not local:
            raise ApiError(400, "\"No password\" only works on the server's own computer; "
                                "turn it on from there (or pick a PIN)")
        self.store.set(mode, secret)
        log.info("web UI sign-in changed to %s", {"none": "no password"}.get(mode, mode))
        with self.lock:
            self.sessions.clear()
            return self._new_session(time.time())

    def reset_to_default(self) -> None:
        self.store.reset()
        log.info("web UI password reset to the default from this computer")
        with self.lock:
            self.sessions.clear()
            self.failures.clear()

    def valid(self, token: str | None, local: bool = False) -> bool:
        if local and self.auth.mode == "none":
            return True
        if not token:
            return False
        with self.lock:
            exp = self.sessions.get(token)
            return exp is not None and exp > time.time()

    def logout(self, token: str | None) -> None:
        with self.lock:
            self.sessions.pop(token or "", None)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind() looks up the host's full DNS name, which can take
        # many seconds on macOS. The name isn't needed, so skip the lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


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
        headers = {"Cache-Control": "no-store", **SECURITY_HEADERS, **(headers or {})}
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        """Stream a file from disk as a download."""
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        quoted = urllib.parse.quote(path.name)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        for k, v in {"Cache-Control": "no-store", **SECURITY_HEADERS}.items():
            self.send_header(k, v)
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

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

    def _cookie(self, token: str) -> str:
        return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}"

    def _local(self) -> bool:
        """A browser on this computer, talking to us directly (not through a proxy)."""
        return is_loopback(self.client_address[0]) and not (
            self.headers.get("X-Forwarded-For") or self.headers.get("Forwarded") or self.headers.get("X-Real-IP"))

    def _host_ok(self) -> bool:
        if host_allowed(self.headers.get("Host"), self.web.hub.web.allowed_hosts):
            return True
        self._send(421, b"This address isn't allowed. If you reach mcsm through a reverse proxy or a custom "
                        b"host name, add it to [web] allowed_hosts in mcsm.toml.\n", "text/plain; charset=utf-8")
        return False

    # ------------------------------------------------------------ routing
    def do_GET(self):
        if not self._host_ok():
            return
        path, _, qs = self.path.partition("?")
        if path in STATIC:
            name, ctype = STATIC[path]
            body = resources.files("mcsm").joinpath("webui", name).read_bytes()
            return self._send(200, body, ctype)
        self._dispatch("GET", path, urllib.parse.parse_qs(qs))

    def do_POST(self):
        if not self._host_ok():
            return
        path, _, qs = self.path.partition("?")
        self._dispatch("POST", path, urllib.parse.parse_qs(qs))

    def _dispatch(self, method: str, path: str, query: dict[str, list[str]]):
        try:
            if not path.startswith("/api/"):
                raise ApiError(404, "not found")
            if method == "POST" and self.headers.get("X-MCSM") != "1":
                raise ApiError(403, "missing X-MCSM header")
            local = self._local()
            if path == "/api/auth" and method == "GET":
                return self._json(200, {**self.web.auth.info(), "local": local})
            if path == "/api/login" and method == "POST":
                token = self.web.login(str(self._body().get("password", "")), self.client_address[0])
                return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie(token)})
            if path == "/api/auth/reset-local" and method == "POST":
                # Forgot the password? Whoever sits at the server's own computer can go back to
                # the default (the same as `mcsm web-password --reset`); never over the network.
                if not local:
                    raise ApiError(403, "this only works in a browser on the server's own computer")
                self.web.reset_to_default()
                return self._json(200, {"ok": True, **self.web.auth.info()})
            if not self.web.valid(self._token(), local):
                raise ApiError(401, "login required")
            if path == "/api/auth/change" and method == "POST":
                b = self._body()
                token = self.web.change(str(b.get("mode", "")), str(b.get("secret", "")), local)
                return self._json(200, {"ok": True, **self.web.auth.info()}, {"Set-Cookie": self._cookie(token)})
            if path == "/api/logout":
                self.web.logout(self._token())
                return self._json(200, {"ok": True},
                                  {"Set-Cookie": f"{SESSION_COOKIE}=; Max-Age=0; Path=/; SameSite=Strict"})
            q = {k: v[-1] for k, v in query.items()}
            if path in LOCAL_ONLY and not local:
                raise ApiError(403, "opening folders only works in a browser on the server's own computer")
            handler = self.web.hub_api.routes.get((method, path))
            if handler is not None:
                if path not in NOTICE_EXEMPT and not notice.accepted(self.web.hub.root):
                    raise ApiError(428, "accept the notice first")
                if path in RAW_UPLOADS:
                    return self._json(200, handler(q, self))
                result = handler(q, self._body() if method == "POST" else {})
                if path == "/api/hub":
                    result = {**result, "local": local}  # whether "Open folder" buttons can work
                return self._json(200, result)
            if m := SERVER_PATH.match(path):
                sid, path = m.group(1), "/api" + m.group(2)
            elif only := self.web.hub.only():  # single server: the short URLs still work
                sid = only[0]
            else:
                raise ApiError(404, "not found")
            if path not in NOTICE_EXEMPT and not notice.accepted(self.web.hub.root):
                raise ApiError(428, "accept the notice first")
            if path in LOCAL_ONLY and not local:
                raise ApiError(403, "opening folders only works in a browser on the server's own computer")
            api = self.web.api_for(sid)
            set_current_server(api.d.server_id)  # so this server's activity feed shows what happens
            handler = api.routes.get((method, path))
            if handler is None:
                raise ApiError(404, "not found")
            if path == "/api/manual/upload":
                return self._json(200, api.upload(self, q))
            if path in RAW_UPLOADS:
                return self._json(200, handler(q, self))
            if path == "/api/export/download":
                return self._send_file(api.export_file(q.get("name", "")))
            if path == "/api/players/skin":
                try:
                    png = api.skins.png(q.get("name", ""))
                except SkinError as e:
                    raise ApiError(404, str(e)) from None
                return self._send(200, png, "image/png", {"Cache-Control": "private, max-age=3600"})
            body = self._body() if method == "POST" else {}
            return self._json(200, handler(q, body))
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except (ConfigError, ModError, JavaError, PlayerError, RuntimeError, ValueError, OSError) as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # pragma: no cover - last resort
            log.exception("web request failed")
            self._json(500, {"error": f"internal error: {e}"})


def setup_options(mojang: Mojang) -> dict:
    """The choices on the setup page."""
    versions, betas, error = [], [], None
    try:
        versions = list(reversed(mojang.releases()))[:40]
        betas = mojang.betas()
    except Exception as e:  # offline: "latest" still works once the network is back
        error = f"couldn't load the list of Minecraft versions: {e}"
    total = setupmod.total_ram_gb()
    return {
        "loaders": [{"name": n, "label": label, "description": desc, "mods": mods}
                    for n, label, desc, mods in setupmod.LOADER_INFO],
        "versions": versions,
        "betas": betas,
        "versions_error": error,
        "total_ram_gb": round(total, 1) if total else None,
        "memory_gb": setupmod.suggested_memory_gb(total),
        "difficulties": setupmod.DIFFICULTIES,
        "gamemodes": setupmod.GAMEMODES,
        "properties_schema": serverprops.schema(),
    }


def search_mods(provider: ModrinthProvider, q: dict, listed: set[str], default_loader: str,
                default_version: str | None = None) -> dict:
    """Modrinth search for the setup and Mods pages; with ``top=1`` and no query, the 20 most popular."""
    query = q.get("q", "").strip()
    top = q.get("top") == "1"
    if not query and not top:
        return {"results": []}
    loader_name = q.get("loader") or default_loader
    from .loaders import LOADERS
    if loader_name not in LOADERS:
        raise ApiError(400, "unknown loader")
    if not LOADERS[loader_name].mod_loaders:
        return {"results": []}  # vanilla: no mods
    version = q.get("version", default_version) or None  # only mods with a build for it
    if version and not re.fullmatch(r"[A-Za-z0-9.+-]{1,32}", version):
        raise ApiError(400, "bad Minecraft version")
    results = provider.search(query, LOADERS[loader_name].mod_loaders, limit=20,
                              index="relevance" if query else "downloads", minecraft=version)
    for r in results:
        r["listed"] = r["id"] in listed or r["slug"] in listed
    return {"results": results}


def mod_requirements(provider: ModrinthProvider, mod_id: str, loaders: tuple[str, ...], minecraft: str | None,
                     channel: str = "release", limit: int = 30) -> dict:
    """Whether a Modrinth mod has a build for ``minecraft`` (any version when None), and the
    mods it needs (their dependencies too), so pickers can select them along with it."""
    def newest(project_id: str):
        ok = [v for v in provider._versions(project_id, loaders) if provider._acceptable(v, channel)
              and (minecraft is None or minecraft in v.get("game_versions", []))]
        return max(ok, key=lambda v: v.get("date_published", "")) if ok else None

    def required(version) -> list[str]:
        return [d["project_id"] for d in version.get("dependencies", [])
                if d.get("dependency_type") == "required" and d.get("project_id")]

    project = provider.project(mod_id)
    info = {"id": project.id, "slug": project.slug, "name": project.name}
    version = newest(project.id)
    if version is None:
        where = f"Minecraft {minecraft}" if minecraft else "this server type"
        return {"project": info, "compatible": False, "reason": f"{project.name} has no build for {where}", "deps": []}
    deps, seen = [], {project.id}
    queue = [(pid, project.name) for pid in required(version)]
    while queue and len(seen) < limit:
        pid, needed_by = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            dep = provider.project(pid)
        except ModError:
            continue
        if dep.server_side == "unsupported":
            continue  # only players need it
        dep_version = newest(dep.id)
        deps.append({"id": dep.id, "slug": dep.slug, "name": dep.name, "needed_by": needed_by,
                     "compatible": dep_version is not None})
        if dep_version is not None:
            queue += [(x, dep.name) for x in required(dep_version)]
    return {"project": info, "compatible": all(d["compatible"] for d in deps), "deps": deps,
            "reason": next((f"it needs {d['name']}, which has no build for Minecraft {minecraft}"
                            for d in deps if not d["compatible"]), "")}


def requirements_query(provider: ModrinthProvider, q: dict, manager=None) -> dict:
    from .loaders import LOADERS
    mod_id = q.get("id", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", mod_id):
        raise ApiError(400, "bad mod id")
    loader = q.get("loader") or (manager.config.server.loader if manager else "")
    if loader not in LOADERS or not LOADERS[loader].mod_loaders:
        raise ApiError(400, "that server type doesn't run mods")
    version = q.get("version")
    if version is None and manager is not None:
        version = manager.lock.minecraft
    return mod_requirements(provider, mod_id, LOADERS[loader].mod_loaders, version or None)


def browse_search(browser, q: dict, manager=None) -> dict:
    from .browse import BrowseError
    kind = q.get("type", "mod")
    loader = q.get("loader") or (manager.config.server.loader if manager else None)
    if loader == "vanilla":
        loader = None
    version = q.get("version")
    if version is None and manager is not None:
        version = manager.lock.minecraft or None
    try:
        return browser.search(q.get("source", "modrinth"), kind, q.get("q", "").strip()[:100], loader or None,
                              version or None, q.get("category") or None, q.get("sort", "relevance"),
                              int(q.get("offset", 0) or 0))
    except BrowseError as e:
        raise ApiError(400, str(e)) from None


def browse_project(browser, q: dict) -> dict:
    from .browse import BrowseError
    pid = q.get("id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid):
        raise ApiError(400, "bad project id")
    try:
        return browser.project(q.get("source", "modrinth"), pid)
    except BrowseError as e:
        raise ApiError(400, str(e)) from None


def browse_categories(browser, q: dict) -> dict:
    from .browse import BrowseError
    try:
        return {"categories": browser.categories(q.get("source", "modrinth"), q.get("type", "mod")),
                "sources": browser.sources}
    except BrowseError as e:
        raise ApiError(400, str(e)) from None


class HubApi:
    """The parts of the API that aren't about one server: the server list, new servers,
    the first-run notice, licenses and mcsm's own updates."""

    def __init__(self, web: WebUI):
        self.web = web
        self.hub = web.hub
        self._mojang: Mojang | None = None
        r: dict[tuple[str, str], Callable[[dict, dict], Any]] = {}
        r[("GET", "/api/hub")] = self.overview
        r[("GET", "/api/hub/setup")] = self.new_server_options
        r[("GET", "/api/hub/mods/requires")] = lambda q, b: requirements_query(ModrinthProvider(self.hub.http), q)
        r[("GET", "/api/hub/mods/search")] = lambda q, b: search_mods(ModrinthProvider(self.hub.http), q, set(), "fabric")
        r[("POST", "/api/hub/create")] = self.create
        r[("POST", "/api/hub/network")] = self.network
        r[("POST", "/api/hub/share")] = self.save_share
        r[("GET", "/api/hub/port")] = self.port_check
        r[("POST", "/api/hub/stage")] = self.stage
        r[("POST", "/api/hub/open")] = self.open_folder
        r[("POST", "/api/hub/quit")] = self.quit
        r[("GET", "/api/hub/curseforge")] = lambda q, b: {"set": bool(self.curseforge_key_now())}
        r[("POST", "/api/hub/curseforge")] = self.save_curseforge
        r[("POST", "/api/hub/share/public-ip")] = self.use_public_ip
        r[("POST", "/api/hub/mods/check")] = self.check_mods
        r[("POST", "/api/hub/trial")] = self.start_trial
        r[("GET", "/api/hub/trial")] = self.trial_status
        r[("POST", "/api/hub/trial/cancel")] = self.cancel_trial
        r[("GET", "/api/hub/saves")] = self.saves
        r[("POST", "/api/hub/import")] = lambda q, b: {"ok": True, "id": self.hub.import_server(str(b.get("id", "")))}
        r[("GET", "/api/hub/browse/search")] = lambda q, b: browse_search(self.browser(), q)
        r[("GET", "/api/hub/browse/project")] = lambda q, b: browse_project(self.browser(), q)
        r[("GET", "/api/hub/browse/categories")] = lambda q, b: browse_categories(self.browser(), q)
        r[("POST", "/api/hub/delete")] = lambda q, b: {
            "ok": True, "message": self.hub.delete(str(b.get("id", "")), b.get("delete_files") is True)}
        r[("GET", "/api/notice")] = lambda q, b: {"accepted": notice.accepted(self.hub.root), "version": notice.NOTICE_VERSION,
                                                  "title": notice.TITLE, "points": notice.POINTS}
        r[("POST", "/api/notice/accept")] = self.accept_notice
        r[("GET", "/api/licenses")] = lambda q, b: licenses.as_dict()
        r[("POST", "/api/self-update/check")] = lambda q, b: {"ok": True, "message": self.hub.check_self_update()}
        r[("POST", "/api/self-update/apply")] = self.apply_self_update
        self.routes = r

    def overview(self, q, b) -> dict:
        hub = self.hub
        return {
            "version": __version__,
            "single": hub.is_single,
            "notice_accepted": notice.accepted(hub.root),
            "self_update": hub.self_update_info(),
            "auth": self.web.auth.info(),
            "home": str(hub.home),
            "network_access": hub.web.host in ("0.0.0.0", "::"),
            "servers": hub.summary(),
            "share": hub.share_status() if not hub.is_single else None,
        }

    def browser(self):
        from .browse import Browser
        return Browser(self.hub.http, self.hub.curseforge_key() if not self.hub.is_single else
                       os.environ.get("MCSM_CURSEFORGE_API_KEY", ""))

    # ------------------------------------------------ try before you buy
    def check_mods(self, q, b) -> dict:
        """The instant check: builds for this version, required mods, declared conflicts."""
        from . import trial
        from .loaders import LOADERS
        loader = str(b.get("loader", ""))
        if loader not in LOADERS or not LOADERS[loader].mod_loaders:
            raise ApiError(400, "that server type doesn't run mods")
        mods = [str(x) for x in (b.get("mods") or []) if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", str(x))]
        minecraft = str(b.get("minecraft") or "") or None
        return trial.check(ModrinthProvider(self.hub.http), LOADERS[loader].mod_loaders, minecraft, mods)

    def start_trial(self, q, b) -> dict:
        """A test boot of a set of mods, in a throwaway server; ``bisect`` finds culprits."""
        from . import trial
        if any(t.state == "running" for t in self.hub.trials.values()):
            raise ApiError(409, "a test is already running; wait for it or stop it")
        sid = b.get("server")
        java_from = None
        if sid:  # the mods of an existing server
            d = self.hub.get(str(sid))
            if d is None:
                raise ApiError(404, "no such server")
            cfg = d.m.config
            loader = cfg.server.loader
            minecraft = d.m.lock.minecraft or cfg.server.minecraft
            mods = [ModSpec(s.source, s.id) for s in cfg.mods]
            java_from = cfg.state_dir / "java"
        else:
            loader = str(b.get("loader", ""))
            minecraft = str(b.get("minecraft") or "latest")
            items = b.get("mods") or []
            if not isinstance(items, list) or len(items) > 200:
                raise ApiError(400, "pick up to 200 mods")
            mods = []
            for item in items:
                source, _, mod_id = str(item).partition(":") if ":" in str(item) else ("modrinth", "", str(item))
                if source not in configmod.MOD_SOURCES or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", mod_id):
                    raise ApiError(400, f"{item!r} isn't a mod id")
                mods.append(ModSpec(source, mod_id))
            java_from = next((dd.m.config.state_dir / "java" for dd in self.hub.daemons.values()
                              if (dd.m.config.state_dir / "java").is_dir()), None)
        if loader not in configmod.LOADERS or loader == "vanilla" and mods:
            raise ApiError(400, "pick a server type that runs mods")
        t = trial.Trial(self.hub, loader, minecraft, mods, bisect=bool(b.get("bisect")), java_from=java_from)
        self.hub.trials = {k: v for k, v in self.hub.trials.items() if v.state == "running"}  # forget old ones
        self.hub.trials[t.id] = t.start()
        return {"ok": True, "id": t.id}

    def trial_status(self, q, b) -> dict:
        t = self.hub.trials.get(q.get("id", ""))
        if t is None:
            raise ApiError(404, "that test isn't running any more")
        return t.to_dict(int(q.get("since", 0) or 0))

    def cancel_trial(self, q, b) -> dict:
        t = self.hub.trials.get(str(b.get("id", "")))
        if t is None:
            raise ApiError(404, "that test isn't running any more")
        t.cancel.set()
        return {"ok": True}

    def use_public_ip(self, q, b) -> dict:
        """Find this network's public address and use it for friends' invite links."""
        if self.hub.is_single:
            raise ApiError(400, "friend downloads need `mcsm start` (the server list)")
        ip = self.hub.public_ip()
        self.hub.save_share(self.hub.share_settings()["port"], ip)
        log.info("friends outside your network now use %s", ip)
        return {"ok": True, "ip": ip, "share": self.hub.share_status()}

    def curseforge_key_now(self) -> str:
        return self.hub.curseforge_key() if not self.hub.is_single else os.environ.get("MCSM_CURSEFORGE_API_KEY", "")

    def save_curseforge(self, q, b) -> dict:
        if self.hub.is_single:
            raise ApiError(400, "set [curseforge] api_key in mcsm.toml for a server run with `mcsm run`")
        self.hub.save_curseforge_key(str(b.get("key", "")))
        log.info("CurseForge API key %s", "saved" if b.get("key") else "removed")
        return {"ok": True, "set": bool(self.hub.curseforge_key())}

    def quit(self, q, b) -> dict:
        """Close mcsm (stopping every server), for when there's no window to close."""
        if self.hub.is_single:
            raise ApiError(400, "this mcsm was started with `mcsm run`; stop it where it runs")
        log.info("quitting (asked from the web UI)")
        threading.Timer(0.5, self.hub.stop_requested.set).start()  # after this reply is sent
        return {"ok": True}

    def saves(self, q, b) -> dict:
        """Singleplayer worlds on this computer, to start a server from (or put on one)."""
        from . import world
        return {"worlds": world.list_saves()}

    def open_folder(self, q, b) -> dict:
        from . import opener
        where = {"home": self.hub.home, "exports": self.hub.exports_dir}.get(str(b.get("what", "")))
        if where is None:
            raise ApiError(400, "unknown folder")
        where.mkdir(parents=True, exist_ok=True)
        if not opener.open_path(where):
            raise ApiError(500, f"couldn't open a file manager; the folder is {where}")
        return {"ok": True, "path": str(where)}

    def stage(self, q, handler) -> dict:
        if handler.headers.get("X-MCSM") != "1":
            raise ApiError(403, "missing X-MCSM header")
        name = q.get("filename", "")
        if not re.fullmatch(r"[A-Za-z0-9 ()\[\]+_.,'-]{1,120}\.(jar|zip)", name):
            raise ApiError(400, "only .jar and .zip files can be added here")
        return self.hub.stage_upload(handler, name, MAX_ARCHIVE if name.endswith(".zip") else MAX_UPLOAD)

    def port_check(self, q, b) -> dict:
        try:
            port = int(q.get("port", ""))
        except ValueError:
            raise ApiError(400, "the port must be a number") from None
        if not 1024 <= port <= 65535:
            raise ApiError(400, "pick a port between 1024 and 65535")
        return self.hub.port_info(port, exclude=q.get("exclude") or None)

    def save_share(self, q, b) -> dict:
        if self.hub.is_single:
            raise ApiError(400, "friend downloads need `mcsm start` (the server list)")
        try:
            port = int(b.get("port", 8766))
        except (TypeError, ValueError):
            raise ApiError(400, "the port must be a number") from None
        if not 1024 <= port <= 65535 or port == self.web.port:
            raise ApiError(400, "pick a port between 1024 and 65535 that the control panel isn't using")
        address = str(b.get("address", "")).strip()
        if address and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:]{2,45}\]|[0-9A-Fa-f:]{2,45}", address):
            raise ApiError(400, "the address should be a host name or IP address, without http:// or a port")
        self.hub.save_share(port, address)
        log.info("friend download settings: port %s, address %s", port, address or "(automatic)")
        return {"ok": True, "share": self.hub.share_status()}

    def new_server_options(self, q, b) -> dict:
        if self._mojang is None:
            self._mojang = Mojang(self.hub.http)
        return {**setup_options(self._mojang), "pending": True, "network_option": False,
                "port": self.hub.free_port(),
                "server_dir": str(self.hub.home / "servers" / "<name>"), "current": None}

    def create(self, q, b) -> dict:
        spec = setupmod.SetupSpec.from_dict(b)
        return {"ok": True, "id": self.hub.create(spec)}

    def network(self, q, b) -> dict:
        if self.hub.is_single:
            raise ApiError(400, "set [web] host in mcsm.toml for `mcsm run`")
        enabled = b.get("enabled") is True
        self.hub.save_web(host="0.0.0.0" if enabled else "127.0.0.1")
        log.info("network access to the control panel turned %s (applies when mcsm restarts)", "on" if enabled else "off")
        return {"ok": True, "restart_needed": enabled != (self.web.host in ("0.0.0.0", "::"))}

    def accept_notice(self, q, b) -> dict:
        if b.get("version") != notice.NOTICE_VERSION:
            raise ApiError(409, "the notice has changed; reload the page")
        notice.accept(self.hub.root, by="web")
        log.info("first-run notice accepted in the web UI")
        return {"ok": True}

    def apply_self_update(self, q, b) -> dict:
        info = self.hub.self_update_info()
        if not info:
            raise ApiError(404, "no mcsm update is available")
        if not info.get("can_install"):
            raise ApiError(400, info.get("reason") or "mcsm can't update itself here")
        if b.get("version") != info["version"]:
            raise ApiError(409, "a different version is available now; reload the page")
        self.hub.run_job(f"update mcsm to {info['version']}", self.hub.apply_self_update)
        return {"ok": True}


class Api:
    """One server's part of the API: /api/servers/<id>/... (or /api/... with a single server)."""

    def __init__(self, web: WebUI, daemon: Daemon, sid: str):
        self.web = web
        self.d = daemon
        self.sid = sid
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
        get("/api/beta", self.betas)
        post("/api/updates/check", lambda q, b: self._job("update check", self.d.check_only, b.get("target")))
        post("/api/updates/apply", lambda q, b: self._job(
            "update", self.d.check_for_updates, True, b.get("target") or None))
        get("/api/setup", self.setup_options)
        post("/api/setup", self.setup_apply)
        get("/api/mods", self.mods)
        get("/api/mods/search", self.search)
        post("/api/mods/add", self.add_mod)
        post("/api/mods/remove", self.remove_mod)
        post("/api/mods/required", self.set_required)
        post("/api/manual/upload", lambda q, b: None)  # handled specially (raw body)
        get("/api/players/skin", lambda q, b: None)    # handled specially (an image)
        get("/api/backups", self.backups)
        post("/api/backups/create", self.create_backup)
        post("/api/backups/restore", self.restore_backup)
        post("/api/open", self.open_folder)
        post("/api/world/replace", self.replace_world)
        post("/api/updates/remove-and-upgrade", self.remove_and_upgrade)
        post("/api/mods/check", lambda q, b: self._check(b, client=False))
        post("/api/client/check", lambda q, b: self._check(b, client=True))
        post("/api/beta/test", self.test_beta)
        get("/api/configs", lambda q, b: configs.grouped(self.m.server_dir, self.m.lock.mods))
        get("/api/configs/file", lambda q, b: configs.read(self.m.server_dir, q.get("path", "")))
        post("/api/configs/file", self.save_config)
        get("/api/export", self.exports)
        post("/api/export", self.export)
        post("/api/export/delete", self.delete_export)
        get("/api/export/download", self.exports)  # streamed by the request handler
        get("/api/players", self.players)
        post("/api/players/action", self.player_action)
        get("/api/java", self.java)
        post("/api/java/install", self.java_install)
        post("/api/java/use", self.java_use)
        get("/api/settings", self.settings)
        post("/api/settings", self.save_settings)
        get("/api/browse/search", lambda q, b: browse_search(self._browser(), q, self.m))
        get("/api/browse/project", lambda q, b: browse_project(self._browser(), q))
        get("/api/browse/categories", lambda q, b: browse_categories(self._browser(), q))
        post("/api/mods/add-many", self.add_many)
        get("/api/mods/requires", lambda q, b: requirements_query(self._modrinth(), q, self.m))
        post("/api/mods/local", self.upload_local)
        get("/api/client", self.client)
        post("/api/client", self.save_client)
        post("/api/client/new-link", self.new_client_link)
        get("/api/client/search", self.client_search)
        self.routes = r
        self.sampler = stats.Sampler()
        self._skins: Skins | None = None

    @property
    def skins(self) -> Skins:
        if self._skins is None or self._skins.server_dir != self.m.server_dir:  # the config may change
            self._skins = Skins(self.m.config.state_dir / "skins", self.m.server_dir)
        return self._skins

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
            "notice_accepted": notice.accepted(self.web.hub.root),
            "setup_pending": d.setup_pending,
            "self_update": self.web.hub.self_update_info(),
            "id": self.sid,
            "auth": self.web.auth.info(),
            "resources": self._resources(),
        }

    def _resources(self) -> dict | None:
        """CPU and memory use of the running server, for the dashboard's bars."""
        proc = self.d.proc
        pid = proc.proc.pid if proc and proc.running and getattr(proc, "proc", None) else None
        usage = self.sampler.read(pid)
        if usage is None:
            return None
        total = setupmod.total_ram_gb()
        return {**usage,
                "memory_max_bytes": stats.heap_bytes(self.m.config.server.memory, setupmod.suggested_memory_gb()),
                "system_memory_bytes": int(total * 1024 ** 3) if total else None}

    def _update_summary(self) -> dict | None:
        c = self.d.last_check
        if not c:
            return None
        return {"checked_at": c["checked_at"], "up_to_date": c["up_to_date"], "target": c["target"],
                "latest": c["latest"], "manual": len(c["manual"]), "lagging": c.get("lagging")}

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

    # --------------------------------------------------------------- setup
    def setup_options(self, q, b) -> dict:
        return {**setup_options(self.m.mojang), "pending": self.d.setup_pending,
                "server_dir": str(self.m.server_dir),
                "network_option": self.web.hub.is_single,  # with several servers it's an mcsm setting
                "network_access": self.m.config.web.host in ("0.0.0.0", "::"),
                "current": self._current_setup()}

    def _current_setup(self) -> dict | None:
        """What an existing mcsm.toml already asks for, so the setup page can start from it."""
        cfg = self.m.config
        mods = [{"slug": s.id, "required": s.required} for s in cfg.mods
                if s.source == "modrinth" and s.id != "fabric-api"]
        if not mods and cfg.server.minecraft == "latest" and cfg.server.loader == "fabric":
            return None  # the untouched default
        mem = re.fullmatch(r"(\d+)G", cfg.server.memory)
        return {"loader": cfg.server.loader, "minecraft": cfg.server.minecraft, "mods": mods,
                "memory_gb": int(mem.group(1)) if mem and 1 <= int(mem.group(1)) <= 64 else None}

    def setup_apply(self, q, b) -> dict:
        if not self.d.setup_pending:
            raise ApiError(409, "this server is already set up")
        spec = setupmod.SetupSpec.from_dict(b)
        spec.world_source = self.web.hub.world_source(spec.world)
        if spec.local_mods:
            self.web.hub.take_staged(spec.local_mods, self.m.mods_dir)
        return self._job("set up server", self.d.run_setup, spec)

    # ---------------------------------------------------------------- mods
    def mods(self, q, b) -> dict:
        lk = self.m.lock
        return {
            "loader": self.m.config.server.loader,
            "minecraft": lk.minecraft,
            "installed": [{"key": x.key, "name": x.name, "version": x.version_number, "filename": x.filename,
                           "source": x.source, "dependency_of": x.dependency_of, "manual": x.manual}
                          for x in lk.mods],
            "configured": self._configured_with_deps(),
            "skipped": [{"key": k, "reason": v} for k, v in lk.skipped.items()],
            "unmanaged": self.m.unmanaged_jars(),
        }

    def _modrinth(self) -> ModrinthProvider:
        return self.m.providers.get("modrinth") or ModrinthProvider(self.m.http)

    def _check(self, b, client: bool) -> dict:
        """The instant check for this server's mods (with the players' mods too, for the Friends page)."""
        from . import trial
        loaders = self.m.loader.mod_loaders
        if not loaders:
            return {"ok": True, "mods": [], "conflicts": [], "problems": [], "minecraft": self.m.lock.minecraft}
        mods = [s.id for s in self.m.config.mods if s.source == "modrinth"]
        if client:
            mods += list(self.m.config.client.mods)
        minecraft = self.m.lock.minecraft or (None if self.m.config.server.minecraft == "latest" else self.m.config.server.minecraft)
        return trial.check(self._modrinth(), loaders, minecraft, mods)

    def _configured_with_deps(self) -> list[dict]:
        """The mods in mcsm.toml, each with the dependencies installed for it (several mods can share one)."""
        lk = self.m.lock
        installed = {x.key: x for x in lk.mods}
        out = []
        for spec in self.m.config.mods:
            key = f"{spec.source}:{spec.id}"
            if key not in installed:
                match = next((x for x in lk.mods if x.source == spec.source and x.project_id == spec.id), None)
                if match:
                    key = match.key
                elif spec.source == "modrinth":
                    try:
                        key = self._modrinth().project(spec.id).key  # a slug in mcsm.toml
                    except Exception:
                        pass
            deps, todo, seen = [], [key], {key}
            while todo:  # follow each installed mod's own list of what it needs
                mod = installed.get(todo.pop(0))
                for pid in (mod.dependencies if mod else []):
                    dk = f"{mod.source}:{pid}"
                    if dk in installed and dk not in seen:
                        seen.add(dk)
                        deps.append({"key": dk, "name": installed[dk].name})
                        todo.append(dk)
            out.append({"source": spec.source, "id": spec.id, "required": spec.required, "key": key,
                        "name": installed[key].name if key in installed else spec.id, "deps": deps})
        return out

    def search(self, q, b) -> dict:
        listed = {s.id for s in self.m.config.mods if s.source == "modrinth"}
        return search_mods(self._modrinth(), q, listed, self.m.config.server.loader, self.m.lock.minecraft)

    def add_mod(self, q, b) -> dict:
        source = b.get("source", "modrinth")
        if source not in configmod.MOD_SOURCES:
            raise ApiError(400, "unknown source")
        mod_id = str(b.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", mod_id):
            raise ApiError(400, "invalid mod id")
        if source != "modrinth" and self.m.loader.mods_folder != "mods":
            raise ApiError(400, "Paper plugins come from Modrinth")
        project = self.m.providers[source].project(mod_id)
        if project.server_side == "unsupported":
            raise ApiError(400, f"{project.name} is client-side only")
        if any(s.source == source and s.id in (mod_id, project.id, project.slug) for s in self.m.config.mods):
            raise ApiError(409, f"{project.name} is already listed")
        deps = []
        if source == "modrinth" and self.m.loader.mod_loaders:
            # Only mods that work on this server's Minecraft, and say what comes along with them.
            try:
                req = mod_requirements(self._modrinth(), project.id, self.m.loader.mod_loaders, self.m.lock.minecraft)
            except (ModError, HttpError) as e:
                log.debug("couldn't check %s's requirements: %s", project.name, e)
                req = None
            if req is not None:
                if not req["compatible"] and self.m.lock.minecraft:
                    raise ApiError(400, f"can't add {project.name}: " + (req["reason"] or "no compatible build"))
                deps = [d["name"] for d in req["deps"]]
        configmod.append_mod(self.m.config.path, ModSpec(source, project.slug or project.id,
                                                         required=bool(b.get("required", True))))
        self.m.reload_config()
        log.info("added %s%s", project.name, f" (with {', '.join(deps)})" if deps else "")
        return {"ok": True, "name": project.name, "deps": deps}

    def _browser(self):
        from .browse import Browser
        return Browser(self.m.http, self.m.config.curseforge_api_key)

    def add_many(self, q, b) -> dict:
        """Add the mods ticked in the mod browser."""
        items = b.get("mods")
        if not isinstance(items, list) or not 0 < len(items) <= 100:
            raise ApiError(400, "pick between 1 and 100 mods")
        added, skipped = [], []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                added.append(self.add_mod(q, {"source": item.get("source", "modrinth"), "id": item.get("id"),
                                              "required": b.get("required", True) is not False})["name"])
            except (ApiError, ModError, ConfigError) as e:
                skipped.append({"name": item.get("name") or item.get("id"), "reason": str(e)})
        return {"ok": True, "added": added, "skipped": skipped}

    def upload_local(self, q, handler) -> dict:
        """A mod jar from this computer ("Local files"). If Modrinth knows it, it becomes a normal
        mod that mcsm keeps up to date; otherwise it stays as your own file in mods/."""
        from .hub import receive
        if handler.headers.get("X-MCSM") != "1":
            raise ApiError(403, "missing X-MCSM header")
        name = q.get("filename", "")
        if not re.fullmatch(r"[A-Za-z0-9 ()\[\]+_.,'-]{1,120}\.jar", name):
            raise ApiError(400, "only .jar files can be added as mods")
        mods_dir = self.m.mods_dir
        dest = receive(handler, mods_dir / name, MAX_UPLOAD)
        try:
            found = self._modrinth().identify([sha1_file(dest)])
        except Exception:
            found = {}
        version = next(iter(found.values()), None)
        if version:
            try:
                r = self.add_mod(q, {"source": "modrinth", "id": version["project_id"]})
                dest.unlink(missing_ok=True)  # mcsm downloads the right file for each version from now on
                return {"ok": True, "name": r["name"], "managed": True}
            except ApiError as e:
                if e.status == 409:  # already one of the server's mods
                    dest.unlink(missing_ok=True)
                    return {"ok": True, "name": name, "managed": True, "already": True}
                log.info("%s is on Modrinth but can't be added (%s); keeping it as a local file", name, e)
        log.info("added %s as a local mod (mcsm won't update it)", name)
        return {"ok": True, "name": name, "managed": False}

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

    # ------------------------------------------------------------- friends
    def _invite_links(self) -> dict:
        """The invite for friends on this network (local) and for everyone else (internet)."""
        c = self.m.config.client
        if not c.token:
            return {}
        from .cli import lan_ip
        from .join import Invite
        share = self.web.hub.share_settings()
        lan = lan_ip()
        out = {"local": Invite(lan, share["port"], c.token).url if lan else None, "internet": None}
        if share["address"]:
            out["internet"] = Invite(share["address"].strip("[]"), share["port"], c.token).url
        return out

    def _invite_link(self) -> str | None:
        links = self._invite_links()
        return links.get("internet") or links.get("local")

    def client(self, q, b) -> dict:
        c = self.m.config.client
        hub = self.web.hub
        preview, error = None, None
        if c.enabled and self.m.lock.installed:
            from .clientpack import PackBuilder
            if getattr(self, "_pack_builder", None) is None or self._pack_builder.m is not self.m:
                self._pack_builder = PackBuilder(self.m)
            try:
                share = hub.share_settings()
                preview = self._pack_builder.build(share["address"] or "<your address>")
            except Exception as e:
                error = str(e)
        return {
            "available": not hub.is_single,
            "enabled": c.enabled, "mods": c.mods, "memory_gb": c.memory_gb,
            "link": self._invite_link() if c.enabled else None,
            "links": self._invite_links() if c.enabled else {},
            "share": hub.share_status() if not hub.is_single else None,
            "pack": preview, "pack_error": error,
            "loader": self.m.config.server.loader,
        }

    def save_client(self, q, b) -> dict:
        if self.web.hub.is_single:
            raise ApiError(400, "friend downloads need `mcsm start` (the server list)")
        path = self.m.config.path
        c = self.m.config.client
        if "enabled" in b:
            configmod.set_value(path, "client", "enabled", "true" if b["enabled"] is True else "false")
            if b["enabled"] is True and not c.token:
                from .clientpack import new_token
                configmod.set_value(path, "client", "token", json.dumps(new_token()))
        if "mods" in b:
            mods = b["mods"]
            if not isinstance(mods, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", x)
                                                     for x in mods):
                raise ApiError(400, "mods must be a list of Modrinth project names")
            configmod.set_value(path, "client", "mods", json.dumps(list(dict.fromkeys(mods))))
        if "memory_gb" in b:
            try:
                memory = int(b["memory_gb"])
            except (TypeError, ValueError):
                raise ApiError(400, "memory must be a number") from None
            if not 1 <= memory <= 32:
                raise ApiError(400, "memory must be between 1 and 32 GB")
            configmod.set_value(path, "client", "memory_gb", str(memory))
        self.m.reload_config()
        self.web.hub.update_share()
        log.info("friend download settings saved")
        return self.client(q, {})

    def new_client_link(self, q, b) -> dict:
        from .clientpack import new_token
        configmod.set_value(self.m.config.path, "client", "token", json.dumps(new_token()))
        self.m.reload_config()
        log.info("made a new invite link; the old one no longer works")
        return self.client(q, {})

    def client_search(self, q, b) -> dict:
        from .loaders import LOADERS
        loaders = LOADERS[self.m.config.server.loader].mod_loaders
        if not loaders or LOADERS[self.m.config.server.loader].mods_folder != "mods":
            return {"results": []}  # vanilla, or Paper: players join with plain Minecraft
        query = q.get("q", "").strip()
        if not query and q.get("top") != "1":
            return {"results": []}
        results = self._modrinth().search(query, loaders, limit=20, index="relevance" if query else "downloads",
                                          side="client", minecraft=self.m.lock.minecraft or (
                                              None if self.m.config.server.minecraft == "latest" else self.m.config.server.minecraft))
        listed = set(self.m.config.client.mods)
        for r in results:
            r["listed"] = r["id"] in listed or r["slug"] in listed
        return {"results": results}

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

    # ---------------------------------------------------------- open folder
    FOLDERS = ("server", "files", "world", "mods", "config", "logs", "crash", "backups", "exports", "manual", "java")

    def folder(self, what: str) -> Path:
        """One of the server's folders, by name (never an arbitrary path)."""
        cfg, sd = self.m.config, self.m.server_dir
        if what == "world":
            return sd / (read_properties(sd / "server.properties").get("level-name") or "world")
        paths = {"server": cfg.root, "files": sd, "mods": sd / "mods", "config": sd / "config", "logs": sd / "logs",
                 "crash": sd / "crash-reports", "backups": cfg.backups.dir, "exports": self.exports_dir,
                 "manual": cfg.manual_dir, "java": cfg.state_dir / "java"}
        if what not in paths:
            raise ApiError(400, "unknown folder")
        return paths[what]

    def open_folder(self, q, b) -> dict:
        from . import opener
        where = self.folder(str(b.get("what", "")))
        if not where.exists():
            if str(b.get("what")) in ("world", "logs", "crash", "java"):
                raise ApiError(404, f"{where} doesn't exist yet (it appears once the server has run)")
            where.mkdir(parents=True, exist_ok=True)
        if not opener.open_path(where):
            raise ApiError(500, f"couldn't open a file manager; the folder is {where}")
        return {"ok": True, "path": str(where)}

    def save_config(self, q, b) -> dict:
        """Save a mod's config file (the previous version is kept in .mcsm/config-backups/)."""
        text = b.get("text")
        if not isinstance(text, str):
            raise ApiError(400, "nothing to save")
        modified = b.get("modified")
        result = configs.write(self.m.server_dir, self.m.config.state_dir / "config-backups", str(b.get("path", "")),
                               text, float(modified) if isinstance(modified, (int, float)) else None)
        log.info("saved %s", b.get("path"))
        return {**result, "running": bool(self.d.proc and self.d.proc.running)}

    def betas(self, q, b) -> dict:
        try:
            versions = self.m.mojang.betas()
        except Exception as e:
            raise ApiError(502, f"couldn't load Minecraft's beta versions: {e}") from None
        return {"betas": versions, "current": self.m.lock.minecraft,
                "copies": not self.web.hub.is_single}

    def test_beta(self, q, b) -> dict:
        """Try a beta Minecraft on a copy of this server; the server itself isn't touched."""
        from . import transfer
        hub = self.web.hub
        if hub.is_single:
            raise ApiError(400, "testing betas needs `mcsm start` (the server list)")
        version = str(b.get("version", ""))
        if version not in self.m.mojang.betas():
            raise ApiError(400, "pick one of the beta versions in the list")
        name = read_properties(self.m.server_dir / "server.properties").get("motd") or self.sid

        def prepare(root: Path) -> None:
            path = root / configmod.CONFIG_NAME
            configmod.set_value(path, "server", "minecraft", json.dumps(version))
            configmod.set_value(path, "updates", "strategy", '"mods-only"')  # stays on the beta
            for spec in configmod.load(root).mods:  # run with whichever mods support the beta
                configmod.remove_mod(path, spec.source, spec.id)
                configmod.append_mod(path, ModSpec(spec.source, spec.id, required=False))

        def run():
            running = self.d.proc and self.d.proc.running
            if running:
                self.d.proc.send("save-off")
                self.d.proc.send("save-all flush")
                time.sleep(5)
            tmp = hub.staging_dir / f"beta-{time.time_ns()}"
            try:
                archive = transfer.export(self.m, tmp / "copy.zip")
            finally:
                if running and self.d.proc and self.d.proc.running:
                    self.d.proc.send("save-on")
            try:
                sid = hub.import_archive(archive, f"{name} (beta {version})", prepare)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            copy = hub.get(sid)
            if copy is None or not copy.submit("install beta", copy.check_for_updates, True, version):
                raise RuntimeError("the copy was made but couldn't start installing; open it and press Update")
            return f"made a copy, \"{name} (beta {version})\", and is installing Minecraft {version} on it"
        return self._job("beta test copy", run)

    def remove_and_upgrade(self, q, b) -> dict:
        lag = (self.d.last_check or {}).get("lagging")
        version, mods = str(b.get("version", "")), b.get("mods")
        if not lag or lag["version"] != version:
            raise ApiError(409, "that's out of date; check for updates and try again")
        allowed = {x["config"] for x in lag["mods"] if x["config"]}
        if not isinstance(mods, list) or not mods or not set(map(str, mods)) <= allowed:
            raise ApiError(400, "pick mods from the list of mods holding the update back")
        return self._job("update", self.d.remove_and_upgrade, version, [str(x) for x in mods])

    def replace_world(self, q, b) -> dict:
        """Swap the server's world for another one (a backup is made first)."""
        from . import world
        if self.d.state != "stopped":
            raise ApiError(409, "stop the server before replacing its world")
        choice = str(b.get("world", ""))
        if not re.fullmatch(r"(save:)?[a-f0-9]{16}", choice):
            raise ApiError(400, "pick a world first")
        source = self.web.hub.world_source(choice)

        def run():
            sd, cfg = self.m.server_dir, self.m.config
            dest = self.folder("world")
            if dest.exists():
                path = backup.create(sd, cfg.backups.dir, "before-new-world", cfg.backups.exclude)
                log.info("backed up the old world to %s", path.name)
            info = world.replace(source, dest)
            if source.parent.parent == self.web.hub.staging_dir:
                shutil.rmtree(source.parent, ignore_errors=True)
            return f"the world is now {info.get('name') or dest.name}; press Start to play it"
        return self._job("replace world", run)

    # --------------------------------------------------------------- export
    @property
    def exports_dir(self) -> Path:
        hub = self.web.hub
        return (hub.exports_dir if not hub.is_single else self.m.config.root / "exports") / self.sid

    def exports(self, q, b) -> dict:
        d = self.exports_dir
        files = sorted(d.glob("*.mcsm.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if d.is_dir() else []
        return {"folder": str(d), "exports": [{"name": p.name, "size": p.stat().st_size, "created": p.stat().st_mtime}
                                               for p in files]}

    def export_file(self, name: str) -> Path:
        path = self.exports_dir / name
        if Path(name).name != name or not name.endswith(".mcsm.zip") or not path.is_file():
            raise ApiError(404, "no such export")
        return path

    def export(self, q, b) -> dict:
        from . import transfer
        include_backups = bool(b.get("backups"))

        def run():
            running = self.d.proc and self.d.proc.running
            if running:  # write everything to disk and hold it there while copying
                self.d.proc.send("save-off")
                self.d.proc.send("save-all flush")
                time.sleep(5)
            try:
                from .properties import read_properties
                name = read_properties(self.m.server_dir / "server.properties").get("motd") or self.sid
                path = transfer.export(self.m, self.exports_dir / transfer.export_name(name), include_backups)
            finally:
                if running and self.d.proc and self.d.proc.running:
                    self.d.proc.send("save-on")
            return f"exported to {path.name}"
        return self._job("export", run)

    def delete_export(self, q, b) -> dict:
        self.export_file(str(b.get("name", ""))).unlink()
        return {"ok": True}

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
            "port": int(read_properties(self.m.server_dir / "server.properties").get("server-port", "25565") or 25565),
            "properties": serverprops.current(read_properties(self.m.server_dir / "server.properties")),
            "properties_schema": serverprops.schema(),
            "choices": {"strategy": configmod.STRATEGIES, "mod_channel": configmod.CHANNELS},
        }

    def save_settings(self, q, b) -> dict:
        b = dict(b)
        advanced = serverprops.validate(b.pop("properties", None))
        port = b.pop("port", None)
        if port is not None:
            try:
                port = int(port)
            except (TypeError, ValueError):
                raise ApiError(400, "the port must be a number") from None
            if not 1024 <= port <= 65535:
                raise ApiError(400, "the port must be between 1024 and 65535")
            clash = self.web.hub.ports(exclude=self.sid).get(port)
            if clash:
                raise ApiError(400, f"port {port} is already used by another server here ({clash})")
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
        props = self.m.server_dir / "server.properties"
        existing = read_properties(props)
        changed = {k: v for k, v in advanced.items() if existing.get(k, serverprops.BY_KEY[k].default) != v}
        if port is not None and str(port) != existing.get("server-port"):
            changed["server-port"] = str(port)
        if changed:
            self.m.server_dir.mkdir(parents=True, exist_ok=True)
            write_properties(props, changed)
        log.info("settings saved")
        return {"ok": True}
