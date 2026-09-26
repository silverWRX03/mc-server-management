"""Working out which mod stopped a server from starting.

Mod loaders and Minecraft's crash reports usually say who's at fault, in their own
words. This reads the server's output (and the newest crash report) for the common
patterns and maps what it finds (mod ids, jar names) to the installed mods:

* Fabric/Quilt: "Incompatible mods found!", "Mod 'X' (x) ... requires ...", "breaks" lines
* NeoForge/Forge: "Missing or unsupported mandatory dependencies", "Mod ID: 'x'",
  "Failed to create mod instance. ModID: x", "... has failed to load correctly"
* Mixins: "Mixin [x.mixins.json:...] from mod x" / "Mixin apply for mod x failed"
* Paper plugins: "Could not load 'plugins/x.jar'", "Error occurred while enabling X"
* crash reports: "Suspected Mod(s):", "Mod File: .../x.jar"
* stack traces naming a jar: "[x.jar:?]" or "(x.jar)"
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# Each rule: a pattern and which group holds a mod id or name ("id") or a jar file ("jar").
RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"Mod '([^']+)' \(([\w.-]+)\)[^\n]*?(requires|is incompatible|breaks|conflicts)", re.I), "id2",
     "its requirements aren't met or it conflicts with another mod"),
    (re.compile(r"[Rr]eplace mod '([^']+)' \(([\w.-]+)\)"), "id2", "the loader says to replace it"),
    (re.compile(r"Remove mod '([^']+)' \(([\w.-]+)\)"), "id2", "the loader says to remove it"),
    (re.compile(r"Install ([\w .'-]+?), version .*?, (?:or later|or earlier)"), "name", "a mod needs it installed"),
    (re.compile(r"Failed to create mod instance\. ModID: ([\w.-]+)"), "id", "it crashed while loading"),
    (re.compile(r"Mod ID: '([\w.-]+)', Requested by: '([\w.-]+)'"), "id2rev", "it's missing a mod it needs"),
    (re.compile(r"(?:mod|Mod) ([\w.-]+) (?:\([^)]*\) )?has failed to load correctly"), "id", "it failed to load"),
    (re.compile(r"Mixin \[[^\]]*\] from mod ([\w.-]+)"), "id", "one of its mixins (code changes) failed"),
    (re.compile(r"Mixin apply for mod ([\w.-]+) failed"), "id", "one of its mixins (code changes) failed"),
    (re.compile(r"Could not load '(?:plugins[/\\])?([^'/\\]+\.jar)'"), "jar", "Paper couldn't load the plugin"),
    (re.compile(r"Error occurred while enabling ([\w.-]+)"), "id", "the plugin failed to start"),
    (re.compile(r"Suspected Mods?: ([^\n]+)"), "list", "the crash report suspects it"),
    (re.compile(r"Mod File: [^\n]*?([^/\\\n]+\.jar)"), "jar", "the crash report points at its file"),
    (re.compile(r"[\[(]([\w.+-]+\.jar)[:\])]"), "jar", "it appears in the error's stack trace"),
]
SKIP_IDS = {"minecraft", "java", "fabricloader", "fabric", "forge", "neoforge", "quilt_loader", "mixinextras", "unknown"}


@dataclass
class Suspect:
    name: str
    reason: str
    evidence: str
    mod_id: str = ""
    filename: str = ""


@dataclass
class Diagnosis:
    suspects: list[Suspect] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.suspects:
            return ""
        names = ", ".join(s.name for s in self.suspects[:3])
        first = self.suspects[0]
        return (f"It looks like {'this mod is' if len(self.suspects) == 1 else 'these mods are'} the problem: {names} "
                f"({first.reason}: \"{first.evidence[:160]}\")")

    def to_dict(self) -> dict:
        return {"summary": self.summary, "suspects": [s.__dict__ for s in self.suspects]}


def _crash_report(server_dir: Path, since: float) -> str:
    folder = server_dir / "crash-reports"
    if not folder.is_dir():
        return ""
    reports = [p for p in folder.glob("crash-*.txt") if p.stat().st_mtime >= since - 5]
    if not reports:
        return ""
    newest = max(reports, key=lambda p: p.stat().st_mtime)
    try:
        return newest.read_text(errors="replace")[:200_000]
    except OSError:
        return ""


def _installed(server_dir: Path, jar: str) -> bool:
    return (server_dir / "mods" / jar).is_file() or (server_dir / "plugins" / jar).is_file()


def diagnose(lines: list[str], server_dir: Path | None = None, mods=(), since: float | None = None) -> Diagnosis:
    """Find the mods to blame. ``mods`` are the installed ModFiles (for names and file names)."""
    from .configs import jar_mods, jars
    by_file = {m.filename: m for m in mods}
    ids: dict[str, str] = {}   # mod id -> jar file name
    names: dict[str, str] = {}  # lower-case name -> jar file name
    for jar in jars(server_dir) if server_dir is not None else []:
        for mod_id, display in jar_mods(jar):
            ids.setdefault(mod_id.lower(), jar.name)
            names.setdefault(display.lower(), jar.name)
    for m in mods:
        names.setdefault(m.name.lower(), m.filename)

    text = "\n".join(lines)
    if server_dir is not None:
        text += "\n" + _crash_report(server_dir, since if since is not None else time.time() - 600)

    found: dict[str, Suspect] = {}

    def add(key: str, reason: str, evidence: str, jar: str = "", mod_id: str = "") -> None:
        key_l = key.lower().strip()
        if not key_l or key_l in SKIP_IDS:
            return
        jar = jar or ids.get(key_l) or names.get(key_l) or ""
        if not jar and key_l.endswith(".jar"):
            jar = key
        mod = by_file.get(jar)
        name = mod.name if mod else (key if not key_l.endswith(".jar") else key.rsplit(".", 1)[0])
        in_mods = bool(jar) and server_dir is not None and _installed(server_dir, jar)
        if not (mod or in_mods or key_l in ids or key_l in names):
            return  # not one of this server's mods (a library, or Minecraft itself)
        ident = jar or key_l
        if ident not in found:
            found[ident] = Suspect(name=name, reason=reason, evidence=evidence.strip(), mod_id=mod_id or key_l, filename=jar)

    for pattern, kind, reason in RULES:
        for m in pattern.finditer(text):
            end = text.find("\n", m.end())
            evidence = text[text.rfind("\n", 0, m.start()) + 1:end if end != -1 else len(text)]
            if kind == "id2":
                add(m.group(2), reason, evidence, mod_id=m.group(2))
            elif kind == "id2rev":  # the mod that asked is the one that can't load
                add(m.group(2), reason + f" ({m.group(1)})", evidence, mod_id=m.group(2))
            elif kind == "id":
                add(m.group(1), reason, evidence, mod_id=m.group(1))
            elif kind == "name":
                add(m.group(1), reason, evidence)
            elif kind == "jar":
                add(m.group(1), reason, evidence, jar=m.group(1) if m.group(1) in by_file or (
                    server_dir is not None and _installed(server_dir, m.group(1))) else "")
            elif kind == "list":
                for part in re.split(r",\s*", m.group(1)):
                    token = re.match(r"\s*([^(]+?)\s*(?:\(([\w.-]+)\))?\s*$", part)
                    if token:
                        add(token.group(2) or token.group(1), reason, evidence)
    return Diagnosis(list(found.values()))
