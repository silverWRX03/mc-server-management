#!/usr/bin/env bash
# Build the Linux executable so it runs on practically any distribution.
#
# A PyInstaller build only runs on systems whose C library (glibc) is at least as new
# as the Python it was built with. So instead of the build machine's Python, this uses
# a portable python-build-standalone Python (installed with uv), which targets glibc
# 2.17: CentOS 7 / RHEL 7 (2014) and everything newer, including Raspberry Pi OS.
# musl-based distributions such as Alpine can't run it; use pipx there.
#
#   packaging/build_linux.sh            # -> dist/mcsm
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
python3 -m pip install --quiet --disable-pip-version-check uv
uv python install --python-preference only-managed "$PYTHON_VERSION"
rm -rf .build-venv
uv venv --quiet --python-preference only-managed --python "$PYTHON_VERSION" .build-venv
uv pip install --quiet --python .build-venv/bin/python "pyinstaller>=6,<7"
.build-venv/bin/pyinstaller --noconfirm packaging/mcsm.spec
echo "built dist/mcsm"
