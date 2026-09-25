"""Entry point for the standalone executable built by PyInstaller (see mcsm.spec)."""

import sys

from mcsm.cli import main

if __name__ == "__main__":
    sys.exit(main())
