# PyInstaller build for the standalone `mcsm` executable.
#   pip install pyinstaller && pyinstaller packaging/mcsm.spec
# Output: dist/mcsm (dist/mcsm.exe on Windows)
import os
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

root = Path(SPECPATH).parent


def license_files():
    """License texts for everything bundled into the executable, shipped inside it."""
    python = next((p for p in (Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
                               Path(sys.base_prefix) / "LICENSE.txt",
                               Path(sys.base_prefix) / "LICENSE") if p.is_file()), None)
    if python is None:
        raise SystemExit("can't find Python's LICENSE.txt; it must be bundled with the executable")
    dist = metadata.distribution("pyinstaller")
    copying = next((Path(dist.locate_file(f)) for f in dist.files or [] if f.name == "COPYING.txt"), None)
    if copying is None or not copying.is_file():
        raise SystemExit("can't find PyInstaller's COPYING.txt; it must be bundled with the executable")
    staged = Path(workpath) / "licenses"
    staged.mkdir(parents=True, exist_ok=True)
    for src, name in ((python, "PYTHON-LICENSE.txt"), (copying, "PYINSTALLER-COPYING.txt"),
                      (root / "LICENSE", "MCSM-LICENSE.txt"), (root / "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md")):
        (staged / name).write_bytes(src.read_bytes())
    return [(str(staged), "mcsm/licenses")]


a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    # The web UI's HTML/JS/CSS, loaded with importlib.resources at runtime.
    datas=[(str(root / "src" / "mcsm" / "webui"), "mcsm/webui"), *license_files()],
    hiddenimports=["mcsm.web"],  # imported lazily by the daemon
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mcsm",
    icon=str(root / "packaging" / "mcsm.ico"),  # drawn by packaging/make_icon.py
    # Windows: no command window; everything happens in the browser (see mcsm/desktop.py).
    # macOS and Linux keep the terminal, where it's started from.
    console=sys.platform != "win32",
    upx=False,          # UPX-packed binaries trigger antivirus false positives
    strip=False,
)
