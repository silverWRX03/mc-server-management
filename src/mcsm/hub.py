"""Several Minecraft servers in one mcsm, run from the web UI: what `mcsm start` opens.

The hub never starts a server on its own; each one waits for its Start button.
Servers live in ``<home>/servers/<id>/`` (each an ordinary mcsm folder with its
own mcsm.toml), plus ``<home>`` itself if it holds a server from mcsm 0.1-0.3,
plus any other folder you started mcsm in. ``mcsm run`` still runs one server
the old way (and starts it); the web UI treats that as a hub with one server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import config as configmod, selfupdate, setup as setupmod
from .config import ConfigError, WebConfig
from .daemon import Daemon, SELF_CHECK_INTERVAL, pid_alive, running_pid, set_current_server
from .http import HttpClient
from .loaders import mods_folder
from .manager import Manager

log = logging.getLogger(__name__)

SERVERS_DIR = "servers"
HUB_FILE = "hub.json"
HOME_ID = "main"           # the server kept directly in the home folder (mcsm 0.1-0.3)
SCAN_EVERY = 5.0


def hub_pid_path(home: Path) -> Path:
    return home / configmod.STATE_DIR / "hub.pid"


def hub_stop_path(home: Path) -> Path:
    return home / configmod.STATE_DIR / "hub-stop-requested"


def running_hub(home: Path) -> int | None:
    try:
        pid = int(hub_pid_path(home).read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if pid != os.getpid() and pid_alive(pid) else None


def _rmtree(path: Path) -> None:
    """Delete a folder, including read-only files (Windows marks some that way)."""
    import shutil
    import stat

    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def receive(handler, dest: Path, max_bytes: int) -> Path:
    """Stream a request body into ``dest`` (atomically)."""
    length = int(handler.headers.get("Content-Length") or 0)
    if not 0 < length <= max_bytes:
        raise ValueError("the file is empty or too large")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.part")
    try:
        with open(tmp, "wb") as out:
            remaining = length
            while remaining:
                chunk = handler.rfile.read(min(1 << 16, remaining))
                if not chunk:
                    raise ValueError("the upload was interrupted")
                out.write(chunk)
                remaining -= len(chunk)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return dest


def port_free(port: int) -> bool:
    """Whether nothing on this computer is listening on a TCP port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "server"


class Hub:
    def __init__(self, home: Path, make_manager: Callable[[configmod.Config], Manager] | None = None,
                 http: HttpClient | None = None, tick: float = 2.0):
        self.home = home.resolve()
        self.http = http or HttpClient()
        self.make_manager = make_manager or (lambda cfg: Manager(cfg, http=self.http, echo=False))
        self.trials: dict = {}  # test boots (trial.Trial) by id
        self.checks: dict = {}  # quick mod checks running in the background (trial.CheckJob) by id
        self.tick = tick
        self._single: Daemon | None = None
        self.daemons: dict[str, Daemon] = {}
        self._threads: dict[str, threading.Thread] = {}
        self.problems: dict[str, dict] = {}      # servers that can't be run here, and why
        self.stop_requested = threading.Event()
        self.restart_requested = False
        self.self_update: dict | None = None
        self.open_browser = False
        self.ui = None
        self.share = None           # the share server for friends' downloads, while one is switched on
        self.share_error: str | None = None
        self._lock = threading.RLock()
        self._web = self._load_web()
        self._apply_curseforge_key()

    @classmethod
    def single(cls, daemon: Daemon) -> Hub:
        """`mcsm run --web`: a hub view of one server run the classic way."""
        hub = cls.__new__(cls)
        hub.home = daemon.m.config.root
        hub.http = daemon.m.http
        hub._single = daemon
        hub.daemons = {HOME_ID: daemon}
        hub.problems = {}
        hub.stop_requested = daemon.stop_requested
        hub._lock = threading.RLock()
        hub.ui = None
        hub.share = None
        hub.share_error = None
        hub.trials = {}
        hub.make_manager = lambda cfg: Manager(cfg, http=hub.http, echo=False)
        return hub

    # --------------------------------------------------------- settings
    @property
    def is_single(self) -> bool:
        return self._single is not None

    @property
    def root(self) -> Path:
        return self.home

    @property
    def state_dir(self) -> Path:
        return self._single.m.config.state_dir if self._single else self.home / configmod.STATE_DIR

    @property
    def web(self) -> WebConfig:
        return self._single.m.config.web if self._single else self._web

    def _hub_file(self) -> dict:
        try:
            data = json.loads((self.state_dir / HUB_FILE).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_hub_file(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_dir / (HUB_FILE + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, self.state_dir / HUB_FILE)

    def _load_web(self) -> WebConfig:
        """Control panel settings: hub.json, else the [web] of a server kept in the home folder."""
        web = WebConfig()
        legacy = self.home / configmod.CONFIG_NAME
        if legacy.exists():
            try:
                old = configmod.load(self.home).web
                web = WebConfig(host=old.host, port=old.port, password=old.password,
                                allowed_hosts=list(old.allowed_hosts))
            except ConfigError:
                pass
        saved = self._hub_file().get("web", {})
        if isinstance(saved, dict):
            web.host = str(saved.get("host", web.host))
            web.port = int(saved.get("port", web.port))
            web.password = str(saved.get("password", web.password))
            web.allowed_hosts = [str(x).lower() for x in saved.get("allowed_hosts", web.allowed_hosts)]
        web.enabled = True
        return web

    def save_web(self, **changes) -> None:
        """Remember control panel settings (they apply the next time mcsm starts)."""
        data = self._hub_file()
        data.setdefault("web", {}).update(changes)
        self._save_hub_file(data)

    # ------------------------------------------------------- CurseForge
    def curseforge_key(self) -> str:
        """The CurseForge API key: saved in mcsm settings, or MCSM_CURSEFORGE_API_KEY."""
        return str(self._hub_file().get("curseforge_api_key") or os.environ.get("MCSM_CURSEFORGE_API_KEY", ""))

    def _apply_curseforge_key(self) -> None:
        # Servers read MCSM_CURSEFORGE_API_KEY when their mcsm.toml has no key of its own.
        key = self._hub_file().get("curseforge_api_key")
        if key:
            os.environ["MCSM_CURSEFORGE_API_KEY"] = key

    def save_curseforge_key(self, key: str) -> None:
        """Check a key with CurseForge, then use it everywhere (empty removes it)."""
        from .mods import curseforge as cf
        key = key.strip()
        if key:
            if not re.fullmatch(r"[A-Za-z0-9$./_+=-]{20,120}", key):
                raise ConfigError("that doesn't look like a CurseForge API key")
            try:
                self.http.get_json(f"{cf.API}/games/{cf.MINECRAFT_GAME_ID}", headers={"x-api-key": key})
            except Exception as e:
                raise ConfigError(f"CurseForge didn't accept that key ({e})") from None
        data = self._hub_file()
        if key:
            data["curseforge_api_key"] = key
            os.environ["MCSM_CURSEFORGE_API_KEY"] = key
        else:
            data.pop("curseforge_api_key", None)
            os.environ.pop("MCSM_CURSEFORGE_API_KEY", None)
        self._save_hub_file(data)
        for d in list(self.daemons.values()):
            try:
                d.m.reload_config()
            except Exception as e:
                log.warning("couldn't reload a server's settings: %s", e)

    # ---------------------------------------------------------- Discord
    def discord(self):
        """The Discord bot for posting invites, or None if none is set up."""
        from .discord import Discord
        token = str(self._hub_file().get("discord", {}).get("token") or "")
        return Discord(self.http, token) if token else None

    def discord_settings(self) -> dict:
        d = self._hub_file().get("discord", {})
        return {"set": bool(d.get("token")), "bot": d.get("bot"), "guild": d.get("guild", ""), "channel": d.get("channel", "")}

    def save_discord_token(self, token: str) -> dict | None:
        """Check a bot token with Discord and keep it (empty removes it). Returns the bot."""
        from .discord import Discord, check_token
        data = self._hub_file()
        if not token.strip():
            data.pop("discord", None)
            self._save_hub_file(data)
            return None
        token = check_token(token)
        bot = Discord(self.http, token).me()
        data["discord"] = {"token": token, "bot": bot}
        self._save_hub_file(data)
        return bot

    def remember_discord_channel(self, guild: str, channel: str) -> None:
        data = self._hub_file()
        if "discord" in data:
            data["discord"].update(guild=guild, channel=channel)
            self._save_hub_file(data)

    # ---------------------------------------------------------- sharing
    def share_settings(self) -> dict:
        """Friends' downloads: the share server's port, and the address friends use (blank = the
        address they opened the invite with)."""
        from .share import DEFAULT_PORT
        s = self._hub_file().get("share", {}) if not self.is_single else {}
        return {"port": int(s.get("port", DEFAULT_PORT)), "address": str(s.get("address", ""))}

    def save_share(self, port: int, address: str) -> None:
        data = self._hub_file()
        data["share"] = {"port": port, "address": address}
        self._save_hub_file(data)
        self.update_share(restart=True)

    def update_share(self, restart: bool = False) -> None:
        """Run the share server while any server has its friend download switched on."""
        if self.is_single:
            return
        from .share import ShareServer
        wanted = any(d.m.config.client.enabled for d in list(self.daemons.values()))
        port = self.share_settings()["port"]
        if self.share and (not wanted or restart or self.share.port != port):
            self.share.stop()
            self.share = None
        if wanted and self.share is None:
            server = ShareServer(self, port)
            try:
                server.start()
                self.share, self.share_error = server, None
            except OSError as e:
                self.share_error = f"port {port} is busy ({e.strerror or e}); pick another in mcsm settings"
                log.warning("couldn't start sharing: %s", self.share_error)

    PUBLIC_IP_SERVICES = ("https://api.ipify.org?format=json", "https://api64.ipify.org?format=json",
                          "https://ifconfig.co/json")

    def public_ip(self) -> str:
        """This network's address on the internet, as other sites see it."""
        import ipaddress
        errors = []
        for url in self.PUBLIC_IP_SERVICES:
            try:
                ip = str(self.http.get_json(url, headers={"Accept": "application/json"}).get("ip", "")).strip()
                addr = ipaddress.ip_address(ip)
            except Exception as e:
                errors.append(str(e))
                continue
            if addr.is_global:
                return str(addr)
        raise RuntimeError("couldn't find your public address (" + (errors[-1] if errors else "no answer") + "); "
                           "check the internet connection, or type it in under mcsm settings → Sharing")

    def share_status(self) -> dict:
        from .cli import lan_ip
        return {**self.share_settings(), "running": bool(self.share), "error": self.share_error, "lan_ip": lan_ip()}

    # ---------------------------------------------------------- servers
    def _extra_roots(self) -> list[Path]:
        return [Path(p) for p in self._hub_file().get("extra", []) if isinstance(p, str)]

    def add_folder(self, root: Path) -> None:
        """Also list a server folder that lives somewhere else (e.g. where mcsm was started)."""
        root = root.resolve()
        if root == self.home or root.parent == self.home / SERVERS_DIR:
            return
        data = self._hub_file()
        extra = [p for p in data.get("extra", []) if isinstance(p, str)]
        if str(root) not in extra:
            data["extra"] = extra + [str(root)]
            self._save_hub_file(data)

    def discover(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        hidden = {Path(p) for p in self._hub_file().get("hidden", []) if isinstance(p, str)}
        if (self.home / configmod.CONFIG_NAME).exists() and self.home not in hidden:
            found[HOME_ID] = self.home
        servers = self.home / SERVERS_DIR
        if servers.is_dir():
            for d in sorted(servers.iterdir()):
                if (d / configmod.CONFIG_NAME).exists() and d.name not in found and d.resolve() not in hidden:
                    found[d.name] = d
        for root in self._extra_roots():
            if (root / configmod.CONFIG_NAME).exists() and root not in found.values() and root not in hidden:
                sid, n = slugify(root.name), 2
                while sid in found:
                    sid, n = f"{slugify(root.name)}-{n}", n + 1
                found[sid] = root
        return found

    def scan(self) -> None:
        """Pick up new server folders, and restart the manager of any server whose manager stopped."""
        if self.is_single:
            return
        with self._lock:
            for sid, root in self.discover().items():
                thread = self._threads.get(sid)
                if sid in self.daemons and thread and thread.is_alive():
                    continue
                self._attach(sid, root)

    def _attach(self, sid: str, root: Path) -> None:
        try:
            m = self.make_manager(configmod.load(root))
        except (ConfigError, OSError, ValueError) as e:
            self.problems[sid] = {"root": str(root), "problem": f"its mcsm.toml has a problem: {e}"}
            self.daemons.pop(sid, None)
            return
        if pid := running_pid(m):
            if pid != os.getpid():
                self.problems[sid] = {"root": str(root), "name": m.config.root.name,
                                      "problem": "another mcsm window (or `mcsm run`) is running this server"}
                self.daemons.pop(sid, None)
                return
        self.problems.pop(sid, None)
        d = Daemon(m, tick=self.tick, autostart=False, server_id=sid, hub_managed=True)
        d.hub = self
        d.web_enabled = True  # problems are shown in the web UI instead of stopping mcsm
        self.daemons[sid] = d
        t = threading.Thread(target=d.run, daemon=True, name=f"server:{sid}")
        self._threads[sid] = t
        t.start()

    def get(self, sid: str) -> Daemon | None:
        return self.daemons.get(sid)

    def only(self) -> tuple[str, Daemon] | None:
        """The one server, when there is exactly one (older single-server URLs use it)."""
        return next(iter(self.daemons.items())) if len(self.daemons) == 1 else None

    def ports(self, exclude: str | None = None) -> dict[int, str]:
        """Minecraft port -> server id, for every server here."""
        from .properties import read_properties
        out = {}
        for sid, d in self.daemons.items():
            if sid != exclude:
                try:
                    out[int(read_properties(d.m.server_dir / "server.properties").get("server-port", "25565"))] = sid
                except ValueError:
                    pass
        return out

    def free_port(self, start: int = 25565) -> int:
        taken = self.ports()
        port = start
        while port in taken or port in self.reserved_ports() or not port_free(port):
            port += 1
        return port

    def reserved_ports(self) -> set[int]:
        """Ports mcsm itself listens on (the control panel and the friends' download)."""
        out = {self.web.port}
        if self.ui is not None and self.ui.httpd is not None:
            out.add(self.ui.httpd.server_address[1])
        if not self.is_single:
            out.add(self.share_settings()["port"])
        return out

    def port_info(self, port: int, exclude: str | None = None) -> dict:
        """Whether a Minecraft port can be used: by another server here, by mcsm, or by another program."""
        from .properties import read_properties
        used_by = None
        sid = self.ports(exclude).get(port)
        if sid is not None and sid in self.daemons:
            used_by = read_properties(self.daemons[sid].m.server_dir / "server.properties").get("motd") or sid
        running_here = sid is not None and sid in self.daemons and bool(
            self.daemons[sid].proc and self.daemons[sid].proc.running)
        return {"port": port, "used_by": used_by, "mcsm": port in self.reserved_ports(),
                "busy": not running_here and not port_free(port), "suggestion": self.free_port()}

    def check_port(self, daemon: Daemon) -> None:
        """Refuse to start a server whose port another running server here already uses."""
        from .properties import read_properties
        port = read_properties(daemon.m.server_dir / "server.properties").get("server-port", "25565")
        for sid, other in self.daemons.items():
            if other is daemon or not (other.proc and other.proc.running):
                continue
            if read_properties(other.m.server_dir / "server.properties").get("server-port", "25565") == port:
                name = read_properties(other.m.server_dir / "server.properties").get("motd") or sid
                raise RuntimeError(f"port {port} is already used by {name}, which is running; "
                                   "stop it first, or give this server another port in its Settings")

    @property
    def staging_dir(self) -> Path:
        return self.state_dir / "staging"

    def stage_upload(self, handler, filename: str, max_bytes: int) -> dict:
        """Keep an upload (from the setup page) until the server it's for is created."""
        import secrets
        cutoff = time.time() - 86400
        if self.staging_dir.is_dir():
            for old in self.staging_dir.iterdir():
                if old.stat().st_mtime < cutoff:
                    shutil.rmtree(old, ignore_errors=True)
        sid = secrets.token_hex(8)
        dest = self.staging_dir / sid / filename
        receive(handler, dest, max_bytes)
        return {"id": sid, "filename": filename, "size": dest.stat().st_size}

    def world_source(self, choice: str) -> Path | None:
        """Where a world picked on the setup page (or Settings) is: an upload, or a singleplayer save."""
        from . import world
        if not choice:
            return None
        if choice.startswith("save:"):
            return world.find_save(choice[5:])
        found = list((self.staging_dir / choice).glob("*.zip")) if re.fullmatch(r"[a-f0-9]{16}", choice) else []
        if not found:
            raise ConfigError("the uploaded world isn't here any more; upload it again")
        return found[0]

    def take_staged(self, stage_ids: list[str], mods_dir: Path) -> int:
        """Move jars picked with "Local files" on the setup page into a server's mods folder."""
        moved = 0
        for stage_id in stage_ids:
            folder = self.staging_dir / stage_id
            for f in folder.glob("*.jar"):
                mods_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), mods_dir / f.name)
                moved += 1
            shutil.rmtree(folder, ignore_errors=True)
        return moved

    def create(self, spec: setupmod.SetupSpec) -> str:
        """Make a new server folder from the setup page and start installing it (not running it)."""
        if self.is_single:
            raise RuntimeError("this mcsm runs a single server (`mcsm run`); use `mcsm start` for several")
        with self._lock:
            base = slugify(spec.motd)
            sid, n = base, 2
            taken = set(self.discover()) | {HOME_ID}
            while sid in taken or (self.home / SERVERS_DIR / sid).exists():
                sid, n = f"{base}-{n}", n + 1
            root = self.home / SERVERS_DIR / sid
            if spec.port in self.ports():
                if spec.port_chosen:
                    other = self.ports()[spec.port]
                    raise ConfigError(f"port {spec.port} is already used by another server here ({other}); pick another")
                spec.port = self.free_port(spec.port)  # two servers can't share a port
            spec.network_access = False  # the hub's own setting decides who can open the panel
            spec.world_source = self.world_source(spec.world)  # before any files are written
            setupmod.configure(root, spec)
            setupmod.mark_pending(root)
            self.take_staged(spec.local_mods, configmod.load(root).server.dir / mods_folder(spec.loader))
            self._attach(sid, root)
            d = self.daemons.get(sid)
            if d is None:
                raise RuntimeError(self.problems.get(sid, {}).get("problem", "the server couldn't be loaded"))
        log.info("creating server %s in %s", sid, root)
        if not d.submit("set up server", d.run_setup, spec):
            raise RuntimeError("the new server is busy; try again")
        return sid

    @property
    def exports_dir(self) -> Path:
        return self.home / "exports"

    def import_server(self, stage_id: str) -> str:
        """Add a server from an export (Settings → Export on another computer)."""
        if self.is_single:
            raise RuntimeError("this mcsm runs a single server (`mcsm run`); use `mcsm start` to import servers")
        if not re.fullmatch(r"[a-f0-9]{16}", stage_id or ""):
            raise ConfigError("that upload isn't here any more; upload the file again")
        found = list((self.staging_dir / stage_id).glob("*.zip"))
        if not found:
            raise ConfigError("that upload isn't here any more; upload the file again")
        sid = self.import_archive(found[0])
        shutil.rmtree(self.staging_dir / stage_id, ignore_errors=True)
        return sid

    def import_archive(self, archive: Path, name: str | None = None,
                       prepare: Callable[[Path], None] | None = None) -> str:
        """A new server from an export (``name`` renames it; ``prepare`` adjusts its folder
        before it's loaded)."""
        from . import transfer
        manifest = transfer.read_manifest(archive)
        with self._lock:
            base = slugify(name or manifest.get("name") or manifest.get("folder") or "imported")
            sid, n = base, 2
            taken = set(self.discover()) | {HOME_ID}
            while sid in taken or (self.home / SERVERS_DIR / sid).exists():
                sid, n = f"{base}-{n}", n + 1
            root = self.home / SERVERS_DIR / sid
            transfer.import_into(archive, root)
            from .properties import read_properties, write_properties
            props_path = configmod.load(root).server.dir / "server.properties"
            if name:
                write_properties(props_path, {"motd": name[:59]})
            port = int(read_properties(props_path).get("server-port", "25565") or 25565)
            if port in self.ports():  # another server here already uses it
                new = self.free_port(port)
                write_properties(props_path, {"server-port": str(new)})
                log.info("the imported server used port %d, which is taken here; it now uses %d", port, new)
            if prepare:
                prepare(root)
            self._attach(sid, root)
        log.info("imported %s into %s", name or manifest.get("name"), root)
        return sid

    def delete(self, sid: str, delete_files: bool) -> str:
        """Take a server off the list; with ``delete_files``, also erase its world, mods,
        backups and settings. Only files mcsm made inside the server's folder are touched."""
        with self._lock:
            d = self.daemons.get(sid)
            if d is None:
                raise RuntimeError("there's no server with that id")
            if (d.proc and d.proc.running) or d.job:
                raise RuntimeError("stop the server (and wait for anything it's doing to finish) first")
            cfg = d.m.config
            root = cfg.root
            d.stop_requested.set()
            t = self._threads.pop(sid, None)
            if t:
                t.join(timeout=30)
            self.daemons.pop(sid, None)
            data = self._hub_file()
            data["extra"] = [p for p in data.get("extra", []) if p != str(root)]
            if not delete_files:
                data["hidden"] = sorted(set(data.get("hidden", [])) | {str(root)})
                self._save_hub_file(data)
                log.info("removed server %s from the list; its files are still in %s", sid, root)
                return f"removed from the list; its files are still in {root}"
            self._save_hub_file(data)
            if root.parent == self.home / SERVERS_DIR:
                _rmtree(root)  # mcsm's own folder for this server
            else:
                # A folder mcsm doesn't own (the home folder, or one you pointed it at): delete
                # only what belongs to the server, and leave anything else there alone.
                inside = [cfg.server.dir, cfg.backups.dir, cfg.manual_dir, root / configmod.CONFIG_NAME,
                          root / "mcsm.lock.json"]
                if root != self.home:
                    inside.append(cfg.state_dir)
                for p in inside:
                    if p is None or not p.exists() or root not in p.parents:
                        continue  # never anything outside the server's folder
                    if p.is_dir():
                        _rmtree(p)
                    else:
                        p.unlink()
                if root == self.home:
                    setupmod.clear_pending(root)
                elif root.exists() and not any(root.iterdir()):
                    root.rmdir()
        log.info("deleted server %s and all of its files (%s)", sid, root)
        return "deleted, with its world, mods and backups"

    def summary(self) -> list[dict]:
        out = []
        for sid, d in list(self.daemons.items()):
            from .properties import read_properties
            props = read_properties(d.m.server_dir / "server.properties")
            lk = d.m.lock
            out.append({
                "id": sid, "name": props.get("motd") or sid, "state": d.state,
                "setup_pending": d.setup_pending, "job": d.job, "minecraft": lk.minecraft,
                "loader": lk.loader or d.m.config.server.loader, "players": len(d.players),
                "max_players": int(props.get("max-players", "20") or 20),
                "port": props.get("server-port", "25565"), "folder": str(d.m.config.root),
                "update": bool(d.last_check and not d.last_check.get("up_to_date") and d.last_check.get("target")),
            })
        for sid, p in self.problems.items():
            out.append({"id": sid, "name": p.get("name") or sid, "state": "unavailable", "problem": p["problem"],
                        "folder": p["root"]})
        return out

    # ------------------------------------------------------ self-update
    def check_self_update(self) -> str:
        if self._single:
            return self._single.check_self_update()
        try:
            release = selfupdate.check(self.http)
        except Exception as e:
            log.debug("mcsm update check failed: %s", e)
            return f"couldn't check for mcsm updates: {e}"
        if release is None:
            self.self_update = None
            return f"mcsm {selfupdate.__version__} is the latest version"
        can, why = selfupdate.install_method(release)
        self.self_update = {**release.to_dict(), "current": selfupdate.__version__, "can_install": can, "reason": why}
        return f"mcsm {release.version} is available"

    def self_update_info(self) -> dict | None:
        return self._single.self_update if self._single else self.self_update

    def apply_self_update(self) -> str:
        """Install the new mcsm, stop every server cleanly, and restart mcsm on the new version."""
        if self._single:
            return self._single.apply_self_update()
        info = self.self_update
        if not info:
            raise RuntimeError("no mcsm update is available")
        release = selfupdate.Release(info["version"], info["tag"], info["url"], info["notes"], info.get("assets", {}))
        message = selfupdate.install(release, http=self.http)
        running = [d for d in self.daemons.values() if d.proc and d.proc.running]
        if any(d.players for d in running):
            for d in running:
                d.proc.say("Server stopping in 1 minute: updating the server manager")
            self.stop_requested.wait(60)
        for d in running:
            d.proc.say("Stopping now!")
        log.info("%s; restarting mcsm", message)
        self.restart_requested = True
        self.stop_requested.set()
        return message

    def run_job(self, name: str, fn: Callable[[], str]) -> None:
        """Run a hub-level task (like installing an mcsm update) in the background."""
        def runner():
            set_current_server(None)
            try:
                fn()
            except Exception as e:
                log.error("%s failed: %s", name, e)
        threading.Thread(target=runner, daemon=True, name=f"hub:{name}").start()

    # --------------------------------------------------------- lifecycle
    def run(self) -> int:
        """Serve the web UI and manage the servers until asked to stop."""
        from .web import WebUI

        state = self.state_dir
        state.mkdir(parents=True, exist_ok=True)
        if pid := running_hub(self.home):
            log.error("mcsm is already running (pid %s)", pid)
            return 1
        hub_pid_path(self.home).write_text(str(os.getpid()))
        hub_stop_path(self.home).unlink(missing_ok=True)
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop_requested.set())
        ui = None
        try:
            self.scan()
            ui = WebUI(self)
            ui.start()
            self.ui = ui
            if self.open_browser:
                import webbrowser
                threading.Timer(1.0, webbrowser.open, args=(ui.url,)).start()
            next_scan, next_self_check = 0.0, time.monotonic() + 30
            while not self.stop_requested.is_set():
                if hub_stop_path(self.home).exists():
                    hub_stop_path(self.home).unlink(missing_ok=True)
                    log.info("stop requested")
                    break
                now = time.monotonic()
                if now >= next_scan:
                    next_scan = now + SCAN_EVERY
                    self.scan()
                    self.update_share()
                if now >= next_self_check:
                    next_self_check = now + SELF_CHECK_INTERVAL
                    self.run_job("mcsm update check", self.check_self_update)
                self.stop_requested.wait(self.tick)
            return 0
        finally:
            if ui:
                ui.stop()
            if self.share:
                self.share.stop()
            self._stop_all()
            hub_pid_path(self.home).unlink(missing_ok=True)

    def _stop_all(self) -> None:
        for d in self.daemons.values():
            d.stop_requested.set()
        for t in self._threads.values():
            t.join(timeout=max((d.m.config.server.stop_timeout for d in self.daemons.values()), default=60) + 30)
