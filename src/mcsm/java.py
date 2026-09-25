"""Picking a Java runtime that can run a given Minecraft version."""

from __future__ import annotations

import re
import shutil
import subprocess

from .config import Config


class JavaError(Exception):
    pass


_VERSION = re.compile(r'version "([^"]+)"')


def parse_major(version_output: str) -> int | None:
    m = _VERSION.search(version_output)
    if not m:
        return None
    parts = m.group(1).split(".")
    if parts[0] == "1" and len(parts) > 1:  # "1.8.0_392" style
        return int(parts[1])
    return int(re.match(r"\d+", parts[0]).group(0))


def probe(binary: str) -> int | None:
    if shutil.which(binary) is None:
        return None
    try:
        out = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_major(out.stderr + out.stdout)


def select(config: Config, required_major: int, probe_fn=probe) -> str:
    """Return a Java binary for ``required_major``.

    An exact entry in ``[java.versions]`` wins, then the lowest configured version
    that is new enough, then ``[java] default`` if it is new enough.
    """
    if required_major in config.java_versions:
        candidates = [config.java_versions[required_major]]
    else:
        candidates = [path for major, path in sorted(config.java_versions.items()) if major >= required_major]
    candidates.append(config.java_default)
    tried = []
    for binary in candidates:
        major = probe_fn(binary)
        tried.append(f"{binary} ({'not found' if major is None else f'Java {major}'})")
        if major is not None and major >= required_major:
            return binary
    raise JavaError(
        f"Minecraft needs Java {required_major}+; tried {', '.join(tried)}. "
        f"Install it and add it under [java.versions] in mcsm.toml")
