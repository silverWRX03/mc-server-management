"""Upgrades wait for every mod; after a month the admin is reminded, and can drop the laggards."""

import time

from mcsm import config as configmod, reminders
from mcsm.config import ModSpec
from mcsm.daemon import Daemon

from test_manager import manager, update


def setup_lagging(make_config, http, modrinth):
    modrinth.project("AAA", "goodmod", "Good Mod")
    modrinth.version("AAA", "1.0", ["1.21.1"])
    modrinth.project("BBB", "slowmod", "Slow Mod")
    modrinth.version("BBB", "1.0", ["1.21.1"])
    cfg = make_config([ModSpec("modrinth", "goodmod"), ModSpec("modrinth", "slowmod", required=False)])
    m = manager(cfg, http, ["1.21.1"])
    assert update(m).ok
    sent = []
    m.notifier.send = sent.append
    # 1.21.2 comes out; Good Mod catches up, Slow Mod (optional) doesn't.
    m.mojang.set_releases(["1.21.1", "1.21.2"])
    modrinth.version("AAA", "1.1", ["1.21.2"])
    return m, sent


def test_optional_mods_hold_back_upgrades_and_the_admin_is_reminded(make_config, http, modrinth):
    m, sent = setup_lagging(make_config, http, modrinth)
    d = Daemon(m, autostart=False)
    d.check_for_updates(allow_stopped=True)
    assert m.lock.minecraft == "1.21.1"  # nothing removed behind the admin's back
    lag = d.last_check["lagging"]
    assert lag["version"] == "1.21.2" and [x["name"] for x in lag["mods"]] == ["Slow Mod"]
    assert lag["mods"][0]["config"] == "modrinth:slowmod" and not lag["mods"][0]["required"]
    # FakeMojang's 1.21.2 came out long ago, so a reminder is due, once per month.
    assert lag["due"] and any("don't support it: Slow Mod" in s for s in sent)
    n = len(sent)
    d.check_for_updates(allow_stopped=True)
    assert not any("don't support it" in s for s in sent[n:])

    # The admin removes it and updates.
    msg = d.remove_and_upgrade("1.21.2", ["modrinth:slowmod"])
    assert m.lock.minecraft == "1.21.2", msg
    assert [s.id for s in configmod.load(m.config.root).mods] == ["goodmod"]


def test_reminder_timing(make_config, http, modrinth, tmp_path):
    m, sent = setup_lagging(make_config, http, modrinth)
    decision, _ = m.check()
    released = m.mojang.release_time("1.21.2")
    assert released and released < time.time()
    day = reminders.DAY
    early = reminders.lagging(m, decision, now=released + 10 * day)
    assert early["days"] == 10 and not early["due"]
    assert reminders.remind(m, early) is None
    month = reminders.lagging(m, decision, now=released + 31 * day)
    assert month["due"] and "a month ago" in reminders.remind(m, month)
    assert reminders.remind(m, reminders.lagging(m, decision, now=released + 45 * day)) is None
    two = reminders.remind(m, reminders.lagging(m, decision, now=released + 61 * day))
    assert two and "2 months ago" in two
    assert len(sent) == 2


def test_remove_and_upgrade_on_the_web(hub_env):
    from test_hub import login
    hub, c = hub_env
    login(c)
    d = hub.get("alpha")
    assert c.post("/api/servers/alpha/updates/remove-and-upgrade", {"version": "9.9", "mods": ["modrinth:x"]})[0] == 409
    d.last_check = {**(d.last_check or {}), "lagging": {"version": "1.21.2", "mods": [{"name": "A", "config": "modrinth:goodmod"}]}}
    status, body, _ = c.post("/api/servers/alpha/updates/remove-and-upgrade", {"version": "1.21.2", "mods": ["modrinth:other"]})
    assert status == 400
