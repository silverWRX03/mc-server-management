from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

from . import __version__, backup, config as configmod, lock as lockmod
from .config import ConfigError, ModSpec
from .daemon import Daemon, request_path, running_pid
from .manager import Manager, UpgradeError
from .mods import ModError, providers_for
from .http import HttpClient, HttpError
from .java import JavaError
from .rcon import Rcon, RconError, read_properties

log = logging.getLogger("mcsm")


def _manager(args) -> Manager:
    return Manager(configmod.load(args.root), echo=not getattr(args, "quiet", False))


def _server_port_open(manager: Manager) -> bool:
    port = int(read_properties(manager.server_dir / "server.properties").get("server-port", "25565"))
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------- commands
def cmd_init(args) -> int:
    root: Path = args.root
    path = root / configmod.CONFIG_NAME
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(configmod.render_template(args.loader, args.minecraft))
    cfg = configmod.load(root)
    if args.server_dir:
        text = path.read_text().replace('dir = "server" ', f'dir = "{Path(args.server_dir).resolve().as_posix()}" ', 1)
        path.write_text(text)
        cfg = configmod.load(root)
    cfg.server.dir.mkdir(parents=True, exist_ok=True)
    if args.accept_eula:
        (cfg.server.dir / "eula.txt").write_text(
            "# Accepted via mcsm init --accept-eula (https://aka.ms/MinecraftEULA)\neula=true\n")
    print(f"wrote {path}")
    if not args.accept_eula and not (cfg.server.dir / "eula.txt").exists():
        print("note: read https://aka.ms/MinecraftEULA, then accept it with `mcsm init --accept-eula --force` "
              "or by writing eula=true to eula.txt in the server directory")
    print("next: add mods with `mcsm add <slug>` (or `mcsm import` for an existing server), then `mcsm update`")
    return 0


def cmd_import(args) -> int:
    m = _manager(args)
    if m.config.server.minecraft == "latest":
        print("set [server] minecraft in mcsm.toml to the version this server runs now, then re-run")
        return 1
    identified, unknown = m.import_existing()
    listed = {(s.source, s.id) for s in m.config.mods}
    added = 0
    for mod in identified:
        if ("modrinth", mod.project_id) in listed:
            continue
        configmod.append_mod(m.config.path, ModSpec("modrinth", mod.project_id))
        added += 1
        print(f"  + {mod.name} ({mod.version_number})")
    print(f"identified {len(identified)} mod(s) on Modrinth, added {added} to mcsm.toml")
    if unknown:
        print("not recognised (left in place, not managed - add them by hand if they are on CurseForge):")
        for name in unknown:
            print(f"  ? {name}")
    return 0


def cmd_add(args) -> int:
    cfg = configmod.load(args.root)
    providers = providers_for(cfg, HttpClient())
    for mod_id in args.ids:
        try:
            project = providers[args.source].project(mod_id)
        except ModError as e:
            print(f"error: {e}")
            return 1
        if project.server_side == "unsupported":
            print(f"skipping {project.name}: it is a client-side only mod")
            continue
        if any(s.source == args.source and s.id in (mod_id, project.id, project.slug) for s in cfg.mods):
            print(f"{project.name} is already listed")
            continue
        configmod.append_mod(cfg.path, ModSpec(args.source, project.slug or project.id, required=not args.optional))
        print(f"added {project.name}{' (optional)' if args.optional else ''}")
    print("run `mcsm check` to see what would be installed")
    return 0


def cmd_remove(args) -> int:
    cfg = configmod.load(args.root)
    ok = configmod.remove_mod(cfg.path, args.source, args.id)
    print(f"removed {args.id}" if ok else f"{args.id} is not listed in mcsm.toml")
    return 0 if ok else 1


def _print_decision(m: Manager, decision, changes) -> None:
    print(f"installed: Minecraft {m.lock.minecraft or '(nothing)'}"
          f"{f' / {m.lock.loader} {m.lock.loader_version}' if m.lock.loader_version else ''}"
          f" - latest release is {decision.latest}")
    for plan in decision.blocked:
        why = [f"{b.name}: {b.reason}" for b in plan.blockers]
        if plan.loader_version is None:
            why.insert(0, f"{plan.loader} has no build for {plan.minecraft} yet")
        print(f"\nMinecraft {plan.minecraft} is blocked by:")
        for line in why:
            print(f"  x {line}")
    if decision.plan:
        p = decision.plan
        for b in p.dropped:
            print(f"  (leaving out {b.name}: {b.reason})")
        if changes is None or changes.empty:
            print(f"\nup to date (Minecraft {p.minecraft})")
        else:
            print(f"\nready to update to Minecraft {p.minecraft} with {p.loader} {p.loader_version}:")
            for line in changes.summary():
                print(f"  {line}")
    else:
        print("\nno installable combination found")
    unmanaged = m.unmanaged_jars()
    if unmanaged:
        print(f"\nwarning: {len(unmanaged)} jar(s) in mods/ are not managed by mcsm and will not be updated:")
        for name in unmanaged:
            print(f"  ! {name}")


def cmd_check(args) -> int:
    m = _manager(args)
    decision, changes = m.check(args.to)
    if args.json:
        out = {
            "installed": m.lock.minecraft,
            "latest": decision.latest,
            "target": decision.plan.minecraft if decision.plan else None,
            "changes": changes.summary() if changes else [],
            "blocked": {p.minecraft: [b.name for b in p.blockers] or [f"{p.loader} loader"]
                        for p in decision.blocked},
        }
        print(json.dumps(out, indent=2))
    else:
        _print_decision(m, decision, changes)
    return 0


def cmd_update(args) -> int:
    m = _manager(args)
    if running_pid(m):
        request_path(m).parent.mkdir(parents=True, exist_ok=True)
        request_path(m).write_text(args.to or "")
        print("mcsm run is managing this server; it will check for updates now and apply them")
        return 0
    if _server_port_open(m):
        print("the server port is in use - stop the server first (or let `mcsm run` manage it)")
        return 1
    decision, changes = m.check(args.to)
    _print_decision(m, decision, changes)
    if not decision.plan or not changes or changes.empty or args.dry_run:
        return 0 if decision.plan else 1
    if not args.yes and sys.stdin.isatty():
        if input("\napply? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    result = m.apply(decision.plan, restart=False)
    print(result.message)
    if result.backup:
        print(f"backup: {result.backup}")
    return 0 if result.ok else 1


def cmd_run(args) -> int:
    return Daemon(_manager(args)).run()


def cmd_stop(args) -> int:
    m = _manager(args)
    pid = running_pid(m)
    if not pid:
        print("mcsm run is not running")
        return 1
    os.kill(pid, signal.SIGTERM)
    print(f"asked mcsm (pid {pid}) to stop the server")
    return 0


def cmd_status(args) -> int:
    m = _manager(args)
    lk = m.lock
    pid = running_pid(m)
    print(f"server dir: {m.server_dir}")
    print(f"daemon:     {'running (pid %s)' % pid if pid else 'not running'}")
    print(f"minecraft:  {lk.minecraft or '(not installed)'}")
    print(f"loader:     {lk.loader or m.config.server.loader} {lk.loader_version or ''}")
    print(f"updated:    {lk.updated_at or '-'}")
    print(f"mods ({len(lk.mods)}):")
    for mod in lk.mods:
        dep = f"  [dependency of {mod.dependency_of}]" if mod.dependency_of else ""
        print(f"  {mod.name:30} {mod.version_number}{dep}")
    for key, reason in lk.skipped.items():
        print(f"  (not installed) {key}: {reason}")
    if lk.failed_plans:
        print("failed updates (will not be retried until something changes):")
        for fp, err in lk.failed_plans.items():
            print(f"  {fp}: {err}")
    return 0


def cmd_cmd(args) -> int:
    m = _manager(args)
    try:
        with Rcon.from_server_dir(m.server_dir) as rcon:
            print(rcon.command(" ".join(args.command)))
    except (RconError, OSError) as e:
        print(f"error: {e}")
        return 1
    return 0


def cmd_backup(args) -> int:
    m = _manager(args)
    if args.list:
        for path in backup.list_backups(m.config.backups.dir):
            print(f"{path.name}  ({path.stat().st_size / 1e6:.1f} MB)")
        return 0
    if running_pid(m) or _server_port_open(m):
        print("warning: the server is running; the backup may catch the world mid-save")
    path = backup.create(m.server_dir, m.config.backups.dir, args.label, m.config.backups.exclude)
    backup.prune(m.config.backups.dir, m.config.backups.keep)
    print(f"created {path}")
    return 0


def cmd_restore(args) -> int:
    m = _manager(args)
    if running_pid(m) or _server_port_open(m):
        print("stop the server first")
        return 1
    backups = backup.list_backups(m.config.backups.dir)
    archive = Path(args.archive) if args.archive else (backups[-1] if backups else None)
    if archive and not archive.exists():
        archive = m.config.backups.dir / args.archive
    if not archive or not archive.exists():
        print("no backup found")
        return 1
    if not args.yes and input(f"replace {m.server_dir} with {archive.name}? [y/N] ").strip().lower() != "y":
        return 1
    backup.restore(archive, m.server_dir)
    print(f"restored {archive.name}")
    print("note: mcsm.lock.json was not changed; run `mcsm update` to re-sync mods with mcsm.toml")
    return 0


# ------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcsm", description="A forever Minecraft server: runs your modded server "
                                "and upgrades it to the newest release once your mods support it.")
    p.add_argument("--version", action="version", version=f"mcsm {__version__}")
    p.add_argument("-C", "--root", type=Path, default=Path("."), help="directory containing mcsm.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create mcsm.toml")
    s.add_argument("--loader", choices=configmod.LOADERS, default="fabric")
    s.add_argument("--minecraft", default="latest", help="initial version (default: newest compatible release)")
    s.add_argument("--server-dir", help="use an existing server directory")
    s.add_argument("--accept-eula", action="store_true", help="accept the Minecraft EULA")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("import", help="adopt the mods already in an existing server's mods/ folder")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("add", help="add mods to mcsm.toml")
    s.add_argument("ids", nargs="+", help="Modrinth slug/id, or CurseForge id/slug with --source curseforge")
    s.add_argument("--source", choices=configmod.MOD_SOURCES, default="modrinth")
    s.add_argument("--optional", action="store_true", help="don't hold back Minecraft upgrades for this mod")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("remove", help="remove a mod from mcsm.toml")
    s.add_argument("id")
    s.add_argument("--source", choices=configmod.MOD_SOURCES, default="modrinth")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("check", help="show available updates and what is blocking newer versions")
    s.add_argument("--to", help="check a specific Minecraft version")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("update", help="apply available updates now (server must be stopped, or use `run`)")
    s.add_argument("--to", help="upgrade to a specific Minecraft version")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("-y", "--yes", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true", help="don't echo server output during the test boot")
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser("run", help="run the server, restart it on crashes and keep it updated")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("stop", help="stop a server started with `mcsm run`")
    s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("status", help="show what is installed")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("cmd", help="send a console command over RCON")
    s.add_argument("command", nargs="+")
    s.set_defaults(fn=cmd_cmd)

    s = sub.add_parser("backup", help="back up the server directory")
    s.add_argument("--label", default="manual")
    s.add_argument("--list", action="store_true")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("restore", help="restore a backup (latest by default)")
    s.add_argument("archive", nargs="?")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_restore)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="[mcsm] %(message)s")
    args.root = args.root.resolve()
    try:
        return args.fn(args)
    except (ConfigError, UpgradeError, ModError, HttpError, JavaError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
