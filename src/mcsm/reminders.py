"""Mods holding back a new Minecraft version, and reminding the admin about them.

Upgrades wait until every mod supports the new version (``updates.wait_for_all_mods``).
If that takes a while, the admin hears about it: once the version has been out for
``updates.remind_days`` (a month), and again every month after, with the list of mods
still not updated. They can then remove those mods and update (web UI, Updates page).
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger(__name__)

DAY = 86400
STATE = "reminders.json"


def lagging(m, decision, now: float | None = None) -> dict | None:
    """The newest release, if mods are what keeps the server off it."""
    latest = decision.latest
    if not latest or m.lock.minecraft == latest or not m.lock.installed:
        return None
    plan = next((p for p in decision.blocked if p.minecraft == latest), None)
    if plan is None or plan.loader_version is None:
        return None  # the loader isn't ready: no mod can be removed to fix that
    mods = [b for b in plan.blockers if b.key != "mcsm:failed" and b.dependency_of is None]
    if not mods:
        return None
    released = None
    try:
        released = m.mojang.release_time(latest)
    except Exception as e:
        log.debug("couldn't tell when %s came out: %s", latest, e)
    now = time.time() if now is None else now
    days = int((now - released) // DAY) if released else 0
    remind = m.config.updates.remind_days
    return {
        "version": latest, "released": released, "days": days, "remind_days": remind,
        "due": bool(released) and days >= remind,
        "mods": [{"name": b.name, "reason": b.reason, "required": b.required, "config": b.config} for b in mods],
    }


def _load(path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remind(m, info: dict | None) -> str | None:
    """Tell the admin (activity log and Discord) when a monthly reminder is due."""
    if not info or not info["due"]:
        return None
    n = info["days"] // info["remind_days"]  # 1 after a month, 2 after two ...
    path = m.config.state_dir / STATE
    state = _load(path)
    if state.get(info["version"], 0) >= n:
        return None
    names = ", ".join(x["name"] for x in info["mods"])
    months = "a month" if n == 1 else f"{n} months"
    message = (f"Minecraft {info['version']} came out {months} ago ({info['days']} days), and these mods still "
               f"don't support it: {names}. The server stays on Minecraft {m.lock.minecraft} until they do. "
               "To update without them, open the Updates page and choose \"Remove them and update\".")
    log.warning(message)
    m.notifier.send(message)
    state = {info["version"]: n}  # older versions don't matter any more
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(STATE + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)
    return message
