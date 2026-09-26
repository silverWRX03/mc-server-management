"""Build step: bake an optional CurseForge API key into the executables.

Like Prism Launcher, mcsm's release builds can carry the project's own CurseForge key (from
the CURSEFORGE_API_KEY repository secret), so people can use CurseForge mods without getting
a key themselves. The key is written to src/mcsm/_buildkeys.py, which git ignores; without
the secret nothing is written and builds work as before. A key someone enters in mcsm
settings (or mcsm.toml, or MCSM_CURSEFORGE_API_KEY) always wins over the built-in one.
"""

import os
import re
from pathlib import Path

key = os.environ.get("CURSEFORGE_API_KEY", "").strip()
target = Path(__file__).resolve().parent.parent / "src" / "mcsm" / "_buildkeys.py"
if not key:
    print("no CURSEFORGE_API_KEY secret: building without a built-in CurseForge key")
elif not re.fullmatch(r"[A-Za-z0-9$./_+=-]{20,120}", key):
    raise SystemExit("CURSEFORGE_API_KEY doesn't look like a CurseForge API key")
else:
    target.write_text(f"# Written by packaging/write_build_keys.py during release builds. Not in git.\n"
                      f"CURSEFORGE_API_KEY = {key!r}\n")
    print("built-in CurseForge key added")
