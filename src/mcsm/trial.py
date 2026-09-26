"""Try before you buy: will a set of mods work together?

Two levels:

* :func:`check` (instant): every mod has a build for the Minecraft version and loader,
  its required mods are available too, and no two mods declare themselves
  incompatible (Modrinth publishes that).
* :class:`Trial` (minutes): installs the mods into a throwaway server with a small flat
  world and boots it, like a real update's test boot. If it doesn't start, it can find
  the culprits by adding the mods back in groups, splitting any group that fails
  until single mods remain, and reports the combination that works, the mods that
  don't, and why. The throwaway server is deleted afterwards.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config as configmod, setup as setupmod
from .config import ModSpec
from .http import HttpError
from .mods.base import ModError
from .mods.modrinth import ModrinthProvider

log = logging.getLogger(__name__)


# ------------------------------------------------------------------- quick check
CHECK_WORKERS = 6


def check(provider: ModrinthProvider, loaders: tuple[str, ...], minecraft: str | None, mod_ids: list[str],
          channel: str = "release", progress=None) -> dict:
    """Problems that can be seen without starting anything.

    Mods are looked up several at a time; one that can't be looked up (Modrinth slow or
    down) is reported as a problem with that mod rather than failing the whole check.
    ``progress(done, total, name)`` is called as each mod is checked."""
    ids = list(dict.fromkeys(mod_ids))

    def one(mod_id: str) -> dict:
        try:
            project = provider.project(mod_id)
            versions = provider._versions(project.id, loaders, minecraft)
        except ModError as e:
            return {"mod": mod_id, "reason": str(e)}
        except HttpError as e:
            return {"mod": mod_id, "reason": f"couldn't check it: {e.friendly}"}
        ok = [v for v in versions if provider._acceptable(v, channel)
              and (not minecraft or minecraft in v.get("game_versions", []))]
        if not ok:
            return {"mod": project.name, "reason": f"no {'/'.join(loaders)} build for Minecraft {minecraft or '(any)'}"}
        version = max(ok, key=lambda v: v.get("date_published", ""))
        return {"project": project, "version": version}

    found: dict[str, dict] = {}
    if ids:
        with ThreadPoolExecutor(max_workers=min(CHECK_WORKERS, len(ids))) as pool:
            futures = {pool.submit(one, mod_id): mod_id for mod_id in ids}
            for n, fut in enumerate(as_completed(futures), 1):
                mod_id = futures[fut]
                found[mod_id] = fut.result()
                if progress:
                    r = found[mod_id]
                    progress(n, len(ids), r["project"].name if "project" in r else r["mod"])

    mods, conflicts, problems = [], [], []
    picked: dict[str, dict] = {}   # project id -> info
    incompatible: dict[str, set[str]] = {}
    for mod_id in ids:  # in the order given
        r = found[mod_id]
        if "project" not in r:
            problems.append(r)
            continue
        project, version = r["project"], r["version"]
        picked[project.id] = {"id": project.id, "slug": project.slug, "name": project.name, "version": version.get("version_number", "")}
        incompatible[project.id] = {d["project_id"] for d in version.get("dependencies", [])
                                    if d.get("dependency_type") == "incompatible" and d.get("project_id")}
        mods.append(picked[project.id])
    for pid, bad in incompatible.items():
        for other in bad & set(picked):
            pair = sorted((picked[pid]["name"], picked[other]["name"]))
            entry = {"mods": pair, "reason": f"{picked[pid]['name']} says it doesn't work with {picked[other]['name']}"}
            if not any(c["mods"] == pair for c in conflicts):
                conflicts.append(entry)
    return {"ok": not conflicts and not problems, "mods": mods, "conflicts": conflicts, "problems": problems,
            "minecraft": minecraft}


class CheckJob:
    """A quick check running in the background, so the page can show which mod it's on."""

    def __init__(self, work):
        self.id = secrets.token_hex(6)
        self.state = "running"
        self.done = self.total = 0
        self.current = ""
        self.result: dict | None = None
        self.error = ""
        self.started = time.time()
        self._work = work
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"check:{self.id}")

    def start(self) -> "CheckJob":
        self.thread.start()
        return self

    def progress(self, done: int, total: int, name: str) -> None:
        self.done, self.total, self.current = done, total, name

    def _run(self) -> None:
        try:
            self.result = self._work(self.progress)
            self.state = "done"
        except HttpError as e:
            self.error = e.friendly
            self.state = "failed"
        except Exception as e:
            log.exception("mod check failed")
            self.error = str(e)
            self.state = "failed"

    def to_dict(self) -> dict:
        return {"id": self.id, "state": self.state, "done": self.done, "total": self.total, "current": self.current,
                "result": self.result, "error": self.error, "elapsed": int(time.time() - self.started)}


# ------------------------------------------------------------------ test boots
class Trial:
    """One test run: boots a throwaway server with ``mods``; with ``bisect``, finds the culprits."""

    def __init__(self, hub, loader: str, minecraft: str, mods: list[ModSpec], bisect: bool = False,
                 java_from: Path | None = None):
        self.id = secrets.token_hex(6)
        self.hub = hub
        self.loader, self.minecraft, self.mods, self.bisect = loader, minecraft, mods, bisect
        self.java_from = java_from
        self.root = hub.state_dir / "trials" / self.id
        self.state = "running"
        self.log: list[str] = []
        self.result: dict | None = None
        self.cancel = threading.Event()
        self.started = time.time()
        self.tests = 0
        self.m = None
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"trial:{self.id}")

    def start(self) -> "Trial":
        self.thread.start()
        return self

    def say(self, text: str) -> None:
        self.log.append(text)
        log.info("test: %s", text)

    def to_dict(self, since: int = 0) -> dict:
        return {"id": self.id, "state": self.state, "log": self.log[since:], "next": len(self.log),
                "tests": self.tests, "result": self.result, "elapsed": int(time.time() - self.started)}

    # ------------------------------------------------------------ internals
    def _prepare(self) -> None:
        spec = setupmod.SetupSpec.from_dict({
            "loader": self.loader, "minecraft": self.minecraft, "motd": "mcsm test", "accept_eula": True,
            "memory_gb": 2, "port": self.hub.free_port(25590),
            "properties": {"level-type": "minecraft:flat", "generate-structures": "false", "view-distance": "3",
                           "simulation-distance": "3", "spawn-protection": "0"}})
        spec.mods = []  # set per test
        setupmod.configure(self.root, spec)
        path = self.root / configmod.CONFIG_NAME
        configmod.set_value(path, "updates", "verify_boot", "true")
        configmod.set_value(path, "backups", "keep", "1")
        # Reuse Java that's already downloaded, instead of fetching it again.
        if self.java_from and self.java_from.is_dir():
            target = self.root / configmod.STATE_DIR / "java"
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(self.java_from, target, target_is_directory=True)
            except OSError:
                pass
        self.m = self.hub.make_manager(configmod.load(self.root))

    def _test(self, mods: list[ModSpec], label: str) -> tuple[bool, str, dict | None]:
        if self.cancel.is_set():
            raise InterruptedError
        self.tests += 1
        self.say(f"Test {self.tests}: {label}…")
        path = self.root / configmod.CONFIG_NAME
        for s in configmod.load(self.root).mods:
            configmod.remove_mod(path, s.source, s.id)
        for s in mods:
            configmod.append_mod(path, ModSpec(s.source, s.id, required=True))
        self.m.reload_config()
        try:
            decision, _ = self.m.check(retry_failed=True)
        except Exception as e:
            return False, f"couldn't work out the versions: {e}", None
        if decision.plan is None:
            blocked = decision.blocked[0] if decision.blocked else None
            why = "; ".join(f"{b.name}: {b.reason}" for b in blocked.blockers) if blocked else "nothing installable"
            if blocked and blocked.loader_version is None:
                why = f"{self.loader} has no build for Minecraft {blocked.minecraft}"
            self.say(f"  ✗ can't be installed: {why}")
            return False, why, None
        result = self.m.apply(decision.plan, verify=True)
        if result.ok:
            self.say(f"  ✓ started fine (Minecraft {decision.plan.minecraft})")
            return True, "", None
        diagnosis = self.m.last_diagnosis.to_dict() if self.m.last_diagnosis else None
        why = (diagnosis or {}).get("summary") or result.message.splitlines()[0][:300]
        self.say(f"  ✗ didn't start: {why}")
        return False, why, diagnosis

    def _run(self) -> None:
        try:
            self._prepare()
            names = {id(s): s.id for s in self.mods}
            ok, why, diagnosis = self._test(self.mods, f"all {len(self.mods)} mod(s) together")
            if ok or not self.bisect:
                self.result = {"ok": ok, "reason": why, "diagnosis": diagnosis,
                               "working": [s.id for s in self.mods] if ok else [], "outliers": [],
                               "bisected": False, "minecraft": self.m.lock.minecraft}
                self.state = "done"
                return
            if not self.mods:
                self.result = {"ok": False, "reason": why, "diagnosis": diagnosis, "working": [], "outliers": [],
                               "bisected": False}
                self.state = "done"
                return
            ok, base_why, _ = self._test([], "Minecraft and the mod loader alone")
            if not ok:
                self.result = {"ok": False, "reason": f"the server doesn't start even without mods: {base_why}",
                               "working": [], "outliers": [], "bisected": True}
                self.state = "done"
                return
            # Suspects last, so splitting reaches them sooner.
            suspects = {(s.get("mod_id") or "").lower() for s in (diagnosis or {}).get("suspects", [])}
            order = sorted(self.mods, key=lambda s: s.id.lower() in suspects)
            kept: list[ModSpec] = []
            outliers: list[dict] = []

            def add(group: list[ModSpec]) -> None:
                label = ", ".join(names[id(s)] for s in group[:4]) + (f" + {len(group) - 4} more" if len(group) > 4 else "")
                ok, why, diag = self._test(kept + group, f"adding {label}")
                if ok:
                    kept.extend(group)
                elif len(group) == 1:
                    outliers.append({"id": group[0].id, "source": group[0].source, "reason": why,
                                     "suspects": [x["name"] for x in (diag or {}).get("suspects", [])]})
                else:
                    half = len(group) // 2
                    add(group[:half])
                    add(group[half:])

            add(order)
            self.result = {"ok": not outliers, "reason": why, "diagnosis": diagnosis, "bisected": True,
                           "working": [s.id for s in kept], "outliers": outliers, "minecraft": self.m.lock.minecraft}
            self.say(f"Done after {self.tests} tests: {len(kept)} mod(s) work together"
                     + (f"; {len(outliers)} don't: " + ", ".join(o['id'] for o in outliers) if outliers else "."))
            self.state = "done"
        except InterruptedError:
            self.say("Stopped.")
            self.state = "cancelled"
        except Exception as e:
            log.exception("test run failed")
            self.say(f"The test itself failed: {e}")
            self.result = {"ok": False, "reason": str(e), "working": [], "outliers": [], "bisected": False}
            self.state = "failed"
        finally:
            if self.m is not None:
                self.m = None
            shutil.rmtree(self.root, ignore_errors=True)
