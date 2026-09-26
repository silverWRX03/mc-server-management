"""Loading and editing ``mcsm.toml``."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

STATE_DIR = ".mcsm"
CONFIG_NAME = "mcsm.toml"
LOADERS = ("fabric", "quilt", "neoforge", "forge", "paper", "vanilla")
MOD_SOURCES = ("modrinth", "curseforge")
STRATEGIES = ("latest-compatible", "latest", "mods-only")
CHANNELS = ("release", "beta", "alpha")


class ConfigError(Exception):
    pass


@dataclass
class ModSpec:
    source: str
    id: str
    required: bool = True
    # Set on specs created automatically for a mod's dependencies.
    dependency_of: str | None = None
    # This mod's own lowest release channel (a mod with only alpha/beta builds), else the server's.
    channel: str | None = None

    @property
    def label(self) -> str:
        return f"{self.source}:{self.id}"


@dataclass
class ServerConfig:
    dir: Path
    loader: str
    minecraft: str
    memory: str = "4G"
    jvm_args: list[str] = field(default_factory=list)
    startup_timeout: int = 600
    stop_timeout: int = 120


@dataclass
class UpdateConfig:
    strategy: str = "latest-compatible"
    mod_channel: str = "release"
    auto_upgrade: bool = True
    check_interval: int = 6 * 3600
    warn_minutes: list[int] = field(default_factory=lambda: [10, 5, 1])
    wait_for_empty: bool = False
    verify_boot: bool = True
    wait_for_all_mods: bool = True   # upgrade Minecraft only once every mod (optional ones too) supports it
    remind_days: int = 30            # then remind about mods still not updated, this often


@dataclass
class BackupConfig:
    dir: Path
    keep: int = 10
    exclude: list[str] = field(default_factory=lambda: ["logs", "crash-reports"])


@dataclass
class WebConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    password: str = ""   # empty = chosen in the web UI (starts as PASSWORD); set here to lock it
    allowed_hosts: list[str] = field(default_factory=list)  # extra host names, e.g. behind a proxy


@dataclass
class ClientConfig:
    """The download friends use to set up Minecraft for this server (see clientpack.py)."""
    enabled: bool = False
    token: str = ""                 # secret part of the invite link
    mods: list[str] = field(default_factory=list)   # extra client-only Modrinth mods (slugs)
    memory_gb: int = 4              # memory the friends' Minecraft gets


@dataclass
class Config:
    root: Path
    server: ServerConfig
    updates: UpdateConfig
    backups: BackupConfig
    mods: list[ModSpec]
    java_default: str = "java"
    java_versions: dict[int, str] = field(default_factory=dict)
    java_version: int | None = None     # force a Java major version; None = what Minecraft needs
    java_auto_install: bool = True      # download Temurin when the needed version isn't available
    java_image: str = "jre"
    manual_dir: Path | None = None      # where to drop mods that must be downloaded by hand
    web: WebConfig = field(default_factory=lambda: WebConfig())
    self_update_check: bool = True      # look for new mcsm releases (installing always asks first)
    discord_webhook: str = ""
    curseforge_api_key: str = ""
    restart_on_crash: bool = True
    client: ClientConfig = field(default_factory=ClientConfig)

    @property
    def path(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR


_DURATION = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")


def parse_duration(value: str | int) -> int:
    """Parse ``"30m"``, ``"6h"``, ``"1d"`` or plain seconds into seconds."""
    if isinstance(value, int):
        return value
    m = _DURATION.match(value)
    if not m:
        raise ConfigError(f"invalid duration: {value!r} (use e.g. 30m, 6h, 1d)")
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _choice(value: str, choices: tuple[str, ...], name: str) -> str:
    if value not in choices:
        raise ConfigError(f"{name} must be one of {', '.join(choices)} (got {value!r})")
    return value


def load(root: Path) -> Config:
    path = root / CONFIG_NAME
    if not path.exists():
        raise ConfigError(f"{path} not found - run `mcsm init` first")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse(root, data)


def _memory(value) -> str:
    """Java heap size: a number with M or G (``4G``, ``4096M``), or ``auto``."""
    text = str(value).strip()
    if text.lower() == "auto":
        return "auto"
    if re.fullmatch(r"\d+[MmGg]", text) and int(text[:-1]) > 0:
        return text.upper()
    raise ConfigError(f"server.memory is {value!r}; use something like 4G, 4096M, or auto")


def _client(c: dict) -> ClientConfig:
    mods = c.get("mods", [])
    if not isinstance(mods, list) or not all(isinstance(m, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", m)
                                             for m in mods):
        raise ConfigError("client.mods must be a list of Modrinth project ids or slugs")
    token = str(c.get("token", ""))
    if token and not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        raise ConfigError("client.token looks wrong; delete it and mcsm makes a new one")
    memory = int(c.get("memory_gb", 4))
    if not 1 <= memory <= 32:
        raise ConfigError("client.memory_gb must be between 1 and 32")
    return ClientConfig(enabled=bool(c.get("enabled", False)), token=token, mods=list(mods), memory_gb=memory)


def parse(root: Path, data: dict) -> Config:
    s = data.get("server", {})
    if "loader" not in s:
        raise ConfigError("[server] loader is required")
    server = ServerConfig(
        dir=(root / s.get("dir", "server")).resolve(),
        loader=_choice(s["loader"], LOADERS, "server.loader"),
        minecraft=str(s.get("minecraft", "latest")),
        memory=_memory(s.get("memory", "4G")),
        jvm_args=list(s.get("jvm_args", [])),
        startup_timeout=parse_duration(s.get("startup_timeout", 600)),
        stop_timeout=parse_duration(s.get("stop_timeout", 120)),
    )

    u = data.get("updates", {})
    updates = UpdateConfig(
        strategy=_choice(u.get("strategy", "latest-compatible"), STRATEGIES, "updates.strategy"),
        mod_channel=_choice(u.get("mod_channel", "release"), CHANNELS, "updates.mod_channel"),
        auto_upgrade=bool(u.get("auto_upgrade", True)),
        check_interval=parse_duration(u.get("check_interval", "6h")),
        warn_minutes=sorted((int(x) for x in u.get("warn_minutes", [10, 5, 1])), reverse=True),
        wait_for_empty=bool(u.get("wait_for_empty", False)),
        verify_boot=bool(u.get("verify_boot", True)),
        wait_for_all_mods=bool(u.get("wait_for_all_mods", True)),
        remind_days=max(1, int(u.get("remind_days", 30))),
    )

    b = data.get("backups", {})
    backups = BackupConfig(
        dir=(root / b.get("dir", "backups")).resolve(),
        keep=int(b.get("keep", 10)),
        exclude=list(b.get("exclude", ["logs", "crash-reports"])),
    )

    mods = []
    for i, m in enumerate(data.get("mods", [])):
        if "id" not in m:
            raise ConfigError(f"mods[{i}] is missing `id`")
        mods.append(ModSpec(
            source=_choice(m.get("source", "modrinth"), MOD_SOURCES, f"mods[{i}].source"),
            id=str(m["id"]),
            required=bool(m.get("required", True)),
            channel=_choice(m["channel"], CHANNELS, f"mods[{i}].channel") if "channel" in m else None,
        ))
    if mods and server.loader == "vanilla":
        raise ConfigError("the vanilla loader cannot run mods; set [server] loader or remove [[mods]]")

    j = data.get("java", {})
    java_versions = {}
    for k, v in j.get("versions", {}).items():
        try:
            java_versions[int(k)] = v
        except ValueError:
            raise ConfigError(f"java.versions keys must be Java major versions (got {k!r})") from None

    forced = j.get("version", "auto")
    if forced != "auto" and not (isinstance(forced, int) or str(forced).isdigit()):
        raise ConfigError(f'[java] version must be "auto" or a major version like 21 (got {forced!r})')

    return Config(
        root=root.resolve(),
        server=server,
        updates=updates,
        backups=backups,
        mods=mods,
        java_default=j.get("default", "java"),
        java_versions=java_versions,
        java_version=None if forced == "auto" else int(forced),
        java_auto_install=bool(j.get("auto_install", True)),
        java_image=_choice(j.get("image", "jre"), ("jre", "jdk"), "java.image"),
        manual_dir=(root / data.get("downloads", {}).get("manual_dir", "manual-downloads")).resolve(),
        self_update_check=bool(data.get("mcsm", {}).get("update_check", True)),
        web=WebConfig(
            enabled=bool(data.get("web", {}).get("enabled", False)),
            host=str(data.get("web", {}).get("host", "127.0.0.1")),
            port=int(data.get("web", {}).get("port", 8765)),
            password=str(data.get("web", {}).get("password", "")),
            allowed_hosts=[str(x).lower() for x in data.get("web", {}).get("allowed_hosts", [])],
        ),
        discord_webhook=data.get("notify", {}).get("discord_webhook", ""),
        curseforge_api_key=(data.get("curseforge", {}).get("api_key", "")
                            or os.environ.get("MCSM_CURSEFORGE_API_KEY", "")),
        restart_on_crash=bool(s.get("restart_on_crash", True)),
        client=_client(data.get("client", {})),
    )


TEMPLATE = """\
# mcsm configuration - https://github.com/silverWRX03/mc-server-management

[server]
dir = "server"                 # server directory (world, config/, mods/, server.properties)
loader = "{loader}"            # fabric | quilt | neoforge | forge | paper | vanilla
minecraft = "{minecraft}"      # version to install on first `mcsm update` ("latest" = newest release)
memory = "4G"
jvm_args = []                  # extra JVM flags, e.g. ["-XX:+UseZGC"]
startup_timeout = "10m"        # how long a boot may take before it counts as failed
stop_timeout = "2m"
restart_on_crash = true

[updates]
# latest-compatible: move to the newest release that the loader and every required mod support
# latest:            only ever move to the newest release, waiting until everything supports it
# mods-only:         never change the Minecraft version, just keep mods updated
strategy = "latest-compatible"
mod_channel = "release"        # lowest mod release channel to accept: release | beta | alpha
auto_upgrade = true            # let `mcsm run` apply upgrades on its own
check_interval = "6h"
warn_minutes = [10, 5, 1]      # in-game countdown before a restart
wait_for_empty = false         # postpone upgrades until nobody is online
verify_boot = true             # boot the upgraded server and roll back if it fails to start
wait_for_all_mods = true       # upgrade Minecraft only when every mod (optional ones too) supports it
remind_days = 30               # a month after a new version is out (and every month after), list the mods holding it back

[backups]
dir = "backups"
keep = 10
exclude = ["logs", "crash-reports"]

[java]
version = "auto"               # "auto" = whatever the Minecraft version needs, or force one, e.g. 21
auto_install = true            # download Eclipse Temurin into .mcsm/java/ when the needed version is missing
image = "jre"                  # jre | jdk
default = "java"               # a system Java to use if it is exactly the right version
# Java you installed yourself, by major version (used before downloading):
# [java.versions]
# 17 = "/usr/lib/jvm/java-17-openjdk/bin/java"
# 21 = "/usr/lib/jvm/java-21-openjdk/bin/java"

[notify]
discord_webhook = ""

[mcsm]
update_check = true            # tell you when a new version of mcsm is out (it never installs without asking)

[web]
enabled = false                # or start with `mcsm run --web`
host = "127.0.0.1"             # only this machine; use "0.0.0.0" behind an HTTPS reverse proxy
port = 8765
password = ""                  # empty = starts as PASSWORD and you choose your own when you sign in
# allowed_hosts = ["mc.example.com"]  # host names used to reach the panel through a reverse proxy

[downloads]
# Some CurseForge authors block third-party downloads. mcsm prints a link for each;
# download the file and drop it in this folder (or straight into mods/), then update again.
manual_dir = "manual-downloads"

[curseforge]
api_key = ""                   # or set MCSM_CURSEFORGE_API_KEY; only needed for curseforge mods

# One [[mods]] block per mod. Dependencies are resolved automatically.
# required = true  -> Minecraft upgrades wait for this mod
# required = false -> the mod is left out of an upgrade if it isn't ready, and comes back when it is
"""


def render_template(loader: str, minecraft: str) -> str:
    return TEMPLATE.format(loader=loader, minecraft=minecraft)


def mod_block(spec: ModSpec) -> str:
    ident = spec.id if spec.id.isdigit() and spec.source == "curseforge" else f'"{spec.id}"'
    channel = f'channel = "{spec.channel}"  # accepts early (unstable) builds\n' if spec.channel in CHANNELS else ""
    return f'\n[[mods]]\nsource = "{spec.source}"\nid = {ident}\nrequired = {str(spec.required).lower()}\n{channel}'


def append_mod(path: Path, spec: ModSpec) -> None:
    text = path.read_text()
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + mod_block(spec))


def remove_mod(path: Path, source: str, mod_id: str) -> bool:
    """Remove a ``[[mods]]`` block, leaving the rest of the file (and its comments) intact."""
    lines = path.read_text().splitlines(keepends=True)
    # Split into chunks that each start at a table header.
    chunks: list[list[str]] = [[]]
    for line in lines:
        if re.match(r"^\s*\[", line):
            chunks.append([])
        chunks[-1].append(line)
    kept, removed = [], False
    for chunk in chunks:
        if chunk and chunk[0].strip() == "[[mods]]":
            body = tomllib.loads("".join(chunk[1:]))
            if str(body.get("id")) == mod_id and body.get("source", "modrinth") == source:
                removed = True
                continue
        kept.append(chunk)
    if removed:
        path.write_text("".join(line for chunk in kept for line in chunk))
    return removed


def set_value(path: Path, table: str, key: str, literal: str) -> None:
    """Set ``key = literal`` inside ``[table]``, keeping every other line (and comment) as is."""
    lines = path.read_text().splitlines(keepends=True)
    header = re.compile(rf"^\s*\[{re.escape(table)}\]\s*(#.*)?$")
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        suffix = "" if not lines or lines[-1].endswith("\n") else "\n"
        path.write_text("".join(lines) + f"{suffix}\n[{table}]\n{key} = {literal}\n")
        return
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
    assign = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)([^#\n]*?)(\s*#.*)?$")
    for i in range(start + 1, end):
        m = assign.match(lines[i].rstrip("\n"))
        if m:
            comment = m.group(3) or ""
            lines[i] = f"{m.group(1)}{literal}{comment}\n"
            break
    else:
        lines.insert(start + 1, f"{key} = {literal}\n")
    path.write_text("".join(lines))
