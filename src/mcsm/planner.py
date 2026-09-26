"""Deciding what the server should be running.

For a candidate Minecraft version the planner checks the loader and resolves every
configured mod plus its required dependencies. A plan is *complete* when the loader
and every required mod are available. Optional mods that are not ready are dropped
from the plan and picked up again by a later update, except that moving an installed
server to a newer Minecraft waits for every mod (``updates.wait_for_all_mods``):
nothing is removed without the admin saying so.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from dataclasses import dataclass, field

from .config import Config, ModSpec
from .loaders import Loader
from .lock import Lock
from .minecraft import Mojang
from .mods import ModError, ModFile, ModProvider, Unavailable
from .mods.base import ClientOnly

log = logging.getLogger(__name__)

# How far back to look for a compatible version when nothing is installed yet.
FIRST_INSTALL_LOOKBACK = 25


@dataclass
class Blocker:
    key: str
    name: str
    reason: str
    required: bool
    dependency_of: str | None = None
    client_only: bool = False
    config: str | None = None    # "source:id" as listed in mcsm.toml, for mods listed there
    waiting: bool = False        # optional, but upgrades wait for every mod (wait_for_all_mods)


@dataclass
class Changes:
    minecraft: tuple[str | None, str] | None = None
    loader: tuple[str | None, str] | None = None
    added: list[ModFile] = field(default_factory=list)
    updated: list[tuple[ModFile, ModFile]] = field(default_factory=list)
    removed: list[ModFile] = field(default_factory=list)

    @property
    def runtime(self) -> bool:
        return self.minecraft is not None or self.loader is not None

    @property
    def empty(self) -> bool:
        return not (self.runtime or self.added or self.updated or self.removed)

    def summary(self) -> list[str]:
        lines = []
        if self.minecraft:
            lines.append(f"Minecraft {self.minecraft[0] or '(none)'} -> {self.minecraft[1]}")
        if self.loader:
            lines.append(f"loader {self.loader[0] or '(none)'} -> {self.loader[1]}")
        lines += [f"+ {m.name} {m.version_number}" for m in self.added]
        lines += [f"~ {new.name} {old.version_number} -> {new.version_number}" for old, new in self.updated]
        lines += [f"- {m.name} {m.version_number}" for m in self.removed]
        return lines


@dataclass
class Plan:
    minecraft: str
    loader: str
    loader_version: str | None
    mods: list[ModFile] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)   # required mods that are not ready
    dropped: list[Blocker] = field(default_factory=list)    # optional / client-only mods left out

    @property
    def complete(self) -> bool:
        return self.loader_version is not None and not self.blockers

    @property
    def fingerprint(self) -> str:
        parts = [self.minecraft, self.loader, self.loader_version or ""]
        parts += sorted(f"{m.key}@{m.version_id}" for m in self.mods)
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]

    def changes(self, lock: Lock) -> Changes:
        c = Changes()
        if lock.minecraft != self.minecraft or not lock.installed:
            c.minecraft = (lock.minecraft, self.minecraft)
        if lock.loader_version != self.loader_version or lock.loader != self.loader:
            c.loader = (lock.loader_version, self.loader_version)
        old = {m.key: m for m in lock.mods}
        new = {m.key: m for m in self.mods}
        for key, m in new.items():
            if key not in old:
                c.added.append(m)
            elif old[key].version_id != m.version_id:
                c.updated.append((old[key], m))
        c.removed = [m for key, m in old.items() if key not in new]
        return c


@dataclass
class Decision:
    plan: Plan | None
    #: newer versions that were considered and why they were not chosen
    blocked: list[Plan] = field(default_factory=list)
    latest: str | None = None


class Planner:
    def __init__(self, config: Config, lock: Lock, mojang: Mojang, loader: Loader,
                 providers: dict[str, ModProvider]):
        self.config = config
        self.lock = lock
        self.mojang = mojang
        self.loader = loader
        self.providers = providers

    def plan_for(self, minecraft: str) -> Plan:
        plan = Plan(minecraft=minecraft, loader=self.loader.name,
                    loader_version=self.loader.latest_version(minecraft))
        channel = self.config.updates.mod_channel
        resolved: dict[str, ModFile] = {}
        failed: dict[str, Blocker] = {}
        client_only: set[str] = set()
        listed: dict[str, str] = {}  # project key -> "source:id" in mcsm.toml
        queue = deque(self.config.mods)

        while queue:
            spec: ModSpec = queue.popleft()
            provider = self.providers[spec.source]
            config_id = f"{spec.source}:{spec.id}" if spec.dependency_of is None else None
            try:
                project = provider.project(spec.id)
            except ModError as e:
                failed[spec.label] = Blocker(spec.label, spec.id, str(e), spec.required, spec.dependency_of,
                                             config=config_id)
                continue
            key = project.key
            if config_id:
                listed.setdefault(key, config_id)
            if key in resolved or key in failed or key in client_only:
                # Something required depends on it, so it is required too.
                if spec.required:
                    if key in resolved:
                        resolved[key].required = True
                    elif key in failed:
                        failed[key].required = True
                continue
            try:
                mod = provider.resolve(spec, minecraft, self.loader.mod_loaders, channel)
            except ClientOnly as e:
                client_only.add(key)
                if spec.dependency_of is None:
                    plan.dropped.append(Blocker(key, project.name, str(e), False, client_only=True, config=config_id))
                continue
            except Unavailable as e:
                failed[key] = Blocker(key, project.name, str(e), spec.required, spec.dependency_of, config=config_id)
                continue
            resolved[key] = mod
            for dep in mod.dependencies:
                queue.append(ModSpec(spec.source, dep, required=spec.required, dependency_of=key))

        # A mod whose dependency is unavailable is unavailable too.
        changed = True
        while changed:
            changed = False
            for key, mod in list(resolved.items()):
                missing = [failed[k] for k in (f"{mod.source}:{d}" for d in mod.dependencies) if k in failed]
                if missing:
                    del resolved[key]
                    failed[key] = Blocker(key, mod.name, f"needs {missing[0].name}: {missing[0].reason}",
                                          mod.required, mod.dependency_of, config=listed.get(key))
                    changed = True

        plan.mods = sorted(resolved.values(), key=lambda m: m.name.lower())
        for b in failed.values():
            (plan.blockers if b.required else plan.dropped).append(b)
        return plan

    def current_version(self) -> str:
        if self.lock.minecraft:
            return self.lock.minecraft
        wanted = self.config.server.minecraft
        return self.mojang.latest_release() if wanted == "latest" else wanted

    def _candidates(self) -> list[str]:
        strategy = self.config.updates.strategy
        if not self.lock.minecraft:
            wanted = self.config.server.minecraft
            if wanted != "latest":
                return [wanted]
            # Fresh install: newest release that works, looking back a limited distance.
            return self.mojang.newer_than(None)[:FIRST_INSTALL_LOOKBACK]
        current = self.lock.minecraft
        if strategy == "mods-only":
            return [current]
        newer = self.mojang.newer_than(current)
        if strategy == "latest":
            return newer[:1] + [current]
        return newer + [current]

    def decide(self, target: str | None = None, retry_failed: bool = False) -> Decision:
        """Pick the plan to apply. ``target`` forces a specific Minecraft version.

        Combinations that failed before are skipped by automatic upgrades, but tried again
        when someone asks (``retry_failed``) or when nothing is installed yet.
        """
        latest = self.mojang.latest_release()
        if target:
            plan = self.plan_for(target)
            return Decision(plan=plan if plan.complete else None, blocked=[] if plan.complete else [plan],
                            latest=latest)
        blocked = []
        skip_failed = self.lock.installed and not retry_failed
        for version in self._candidates():
            plan = self.plan_for(version)
            if self.config.updates.wait_for_all_mods and self.lock.installed and version != self.lock.minecraft:
                # A newer Minecraft waits for every mod, optional ones too (client-only ones never
                # run on the server, so they don't count).
                for b in [b for b in plan.dropped if not b.client_only]:
                    plan.dropped.remove(b)
                    b.waiting = True
                    plan.blockers.append(b)
            if skip_failed and plan.complete and plan.fingerprint in self.lock.failed_plans:
                log.info("not retrying Minecraft %s automatically: the same update failed before", version)
                plan.blockers.append(Blocker("mcsm:failed", "an earlier attempt",
                                             f"it failed ({self.lock.failed_plans[plan.fingerprint]}); "
                                             "update manually to try again", True))
            if plan.complete:
                return Decision(plan=plan, blocked=blocked, latest=latest)
            blocked.append(plan)
        return Decision(plan=None, blocked=blocked, latest=latest)
