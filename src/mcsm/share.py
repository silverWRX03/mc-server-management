"""The share server: the one part of mcsm meant for friends on the internet.

It listens on its own port (8766 by default; forward it on your router along with the
Minecraft port) and serves only, per server with a friend download switched on:

* ``/join/<secret>``: a page with download buttons;
* ``/join/<secret>/pack.json``: what the friend's Minecraft needs (clientpack.py);
* ``/join/<secret>/download/<file>``: mcsm itself, named so it knows where to connect;
* ``/join/<secret>/mods/<file>``: your own mod files for players (the server's client-mods folder).

There is no sign-in and nothing to change here; the secret in the link keeps servers
from being found by guessing. The control panel stays on its own, private port.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import re
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import quote, unquote

from . import __version__, selfupdate
from .clientpack import PackBuilder
from .join import Invite, download_name
from .properties import read_properties

log = logging.getLogger(__name__)

DEFAULT_PORT = 8766
ASSETS = {"windows": "mcsm-windows-x64.exe", "macos": "mcsm-macos-arm64", "linux": "mcsm-linux-x64",
          "linux-arm64": "mcsm-linux-arm64"}
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _is_local(host: str) -> bool:
    import ipaddress
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host.endswith(".local") or host == "localhost"
    return addr.is_private or addr.is_loopback or addr.is_link_local


class ShareServer:
    def __init__(self, hub, port: int = DEFAULT_PORT, host: str = "0.0.0.0"):
        self.hub = hub
        self.port = port
        self.host = host
        self.httpd = None
        self._builders: dict[str, tuple[object, PackBuilder]] = {}
        self._download_lock = threading.Lock()

    # ---------------------------------------------------------- lookups
    def server_for(self, token: str):
        for sid, d in list(self.hub.daemons.items()):
            c = d.m.config.client
            if c.enabled and c.token and hmac.compare_digest(c.token.encode(), token.encode()):
                return sid, d
        return None, None

    def builder(self, sid: str, d) -> PackBuilder:
        cached = self._builders.get(sid)
        if cached is None or cached[0] is not d.m:
            cached = self._builders[sid] = (d.m, PackBuilder(d.m))
        return cached[1]

    def address(self, d, request_host: str) -> str:
        """host:port players connect Minecraft to."""
        # A friend who opened the invite through this computer's local address is on the same
        # network, so they join through it too; everyone else uses the public address.
        public = request_host if _is_local(request_host) else \
            (self.hub.share_settings().get("address") or "").strip() or request_host
        port = read_properties(d.m.server_dir / "server.properties").get("server-port", "25565")
        return public if port == "25565" else f"{public}:{port}"

    def binary(self, asset: str) -> Path:
        """This version of mcsm for the friend's computer: ourselves, or from GitHub (verified)."""
        if selfupdate.frozen() and asset == selfupdate.asset_name():
            return Path(sys.executable)
        cached = self.hub.state_dir / "downloads" / f"{__version__}-{asset}"
        with self._download_lock:
            if not cached.exists():
                release = selfupdate.release_for(self.hub.http)
                if release is None:
                    raise FileNotFoundError(f"mcsm {__version__} isn't published on GitHub")
                selfupdate.fetch_verified(release, asset, cached, self.hub.http)
        return cached

    # ------------------------------------------------------------ server
    def start(self) -> None:
        from .web import _Server
        share = self

        class Handler(ShareHandler):
            server_ref = share
        self.httpd = _Server((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="share").start()
        log.info("sharing with friends on port %s", self.port)

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None


class ShareHandler(BaseHTTPRequestHandler):
    server_ref: ShareServer
    server_version = f"mcsm/{__version__}"

    def log_message(self, fmt, *args):
        log.debug("share: " + fmt, *args)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, status: int, message: str) -> None:
        self._send(status, (message + "\n").encode(), "text/plain; charset=utf-8")

    def _request_host(self) -> tuple[str, int]:
        raw = (self.headers.get("Host") or "").strip()
        m = re.fullmatch(r"\[?([A-Za-z0-9.:-]{1,253}?)\]?(?::(\d{1,5}))?", raw)
        host = m.group(1) if m else "localhost"
        port = int(m.group(2)) if m and m.group(2) else self.server_ref.port
        return host, port

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        m = re.fullmatch(r"/join/([A-Za-z0-9_-]{16,64})(/pack\.json|/download/([a-z0-9-]+)|/mods/([^/]{1,400}))?/?",
                         self.path.split("?")[0])
        if not m:
            return self._text(404, "Nothing here. Ask the server's owner for an invite link.")
        token = m.group(1)
        sid, d = self.server_ref.server_for(token)
        if d is None:
            return self._text(404, "This invite isn't valid any more. Ask the server's owner for a new link.")
        host, port = self._request_host()
        try:
            pack = self.server_ref.builder(sid, d).build(self.server_ref.address(d, host))
        except Exception as e:
            log.warning("couldn't build the friend download for %s: %s", sid, e)
            return self._text(503, f"The server isn't ready for players yet ({e}). Try again later.")
        invite = Invite(host, port, token)
        if m.group(2) == "/pack.json":
            # Your own files come from here: point them at this address, as the friend reached it.
            mods = [{**x, "url": f"{invite.url}/mods/{quote(x['filename'])}"} if x.get("local") else x
                    for x in pack.get("mods", [])]
            return self._send(200, json.dumps({**pack, "mods": mods}).encode(), "application/json")
        if m.group(4):
            from .clientpack import JAR_NAME, local_jars
            name = unquote(m.group(4))
            jar = next((p for p in local_jars(d.m.config) if p.name == name), None) if JAR_NAME.fullmatch(name) else None
            if jar is None:
                return self._text(404, "No such file.")
            self.send_response(200)
            for k, v in {**HEADERS, "Content-Type": "application/java-archive", "Content-Length": str(jar.stat().st_size)}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                with open(jar, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, 1 << 16)
            return None
        if m.group(3):
            asset = ASSETS.get(m.group(3))
            if not asset:
                return self._text(404, "Unknown download.")
            try:
                path = self.server_ref.binary(asset)
            except Exception as e:
                log.warning("couldn't provide %s: %s", asset, e)
                return self._text(503, f"That download isn't available right now ({e}).")
            name = download_name(pack["name"], invite, asset)
            self.send_response(200)
            for k, v in {**HEADERS, "Content-Type": "application/octet-stream",
                         "Content-Length": str(path.stat().st_size),
                         "Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                with open(path, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, 1 << 16)
            log.info("%s downloaded the setup for %s (%s)", self.client_address[0], pack["name"], asset)
            return None
        return self._send(200, page(pack, invite).encode(), "text/html; charset=utf-8")


def page(pack: dict, invite: Invite) -> str:
    e = html.escape
    loader = "plain Minecraft" if pack["loader"] == "vanilla" else f"{pack['loader'].capitalize()}"
    n = len(pack.get("mods", []))
    icon = f'<img src="{e(pack["icon"])}" alt="" width="64" height="64">' if pack.get("icon") else ""
    btn = lambda os_key, label: f'<a class="btn" href="/join/{e(invite.token)}/download/{os_key}">{label}</a>'  # noqa: E731
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Join {e(pack['name'])}</title>
<style>
body{{margin:0;background:#0f1214;color:#e6e9ec;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:640px;margin:0 auto;padding:32px 20px}}
.head{{display:flex;gap:16px;align-items:center}} img{{border-radius:8px;image-rendering:pixelated}}
h1{{margin:0;font-size:26px}} .muted{{color:#8b949e}} code{{background:#1e2328;padding:2px 6px;border-radius:4px;overflow-wrap:anywhere}}
.card{{background:#171b1f;border:1px solid #2a3036;border-radius:10px;padding:18px 20px;margin-top:18px}}
.btns{{display:flex;gap:10px;flex-wrap:wrap;margin-top:6px}}
.btn{{display:inline-block;background:#4a9f4a;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:600}}
.btn:hover{{background:#5cb85c}} ol{{padding-left:20px}} li{{margin:6px 0}}
</style></head><body><main>
<div class="head">{icon}<div><h1>{e(pack['name'])}</h1>
<div class="muted">Minecraft {e(pack['minecraft'])} · {e(loader)}{f" · {n} mods" if n else ""}</div></div></div>
<div class="card"><strong>Download the setup for your computer</strong>
<div class="btns">{btn("windows", "Windows")}{btn("macos", "Mac (Apple silicon)")}{btn("linux", "Linux")}</div>
<ol>
<li>You need Minecraft: Java Edition and a launcher: the official <strong>Minecraft Launcher</strong>,
<strong>Prism Launcher</strong>, the <strong>Modrinth App</strong> or <strong>CurseForge</strong>.</li>
<li>Run the file you downloaded. A page opens where you tick your launchers; it adds <strong>{e(pack['name'])}</strong>
to each with the right Minecraft version and mods (your other worlds and installations aren't touched).</li>
<li>In your launcher, pick <strong>{e(pack['name'])}</strong> and press Play.</li>
</ol>
<p class="muted">Windows may say it "protected your PC", because this free app isn't code-signed: choose
<em>More info → Run anyway</em>. On a Mac, right-click the file and choose <em>Open</em>. On Linux, run
<code>chmod +x</code> on it first. Run it again whenever the server updates, to update your mods.</p>
</div>
<div class="card"><strong>Already have mcsm?</strong>
<p>Run <code>mcsm join {e(invite.url)}</code></p>
<p class="muted">Server address, if you'd rather set things up yourself: <code>{e(pack['address'])}</code></p>
</div>
</main></body></html>"""
