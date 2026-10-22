"""Entry point PyInstaller freezes into ClawdTank.exe (same as ``python -m clawd_tank_tray``)."""

import sys

from clawd_tank_tray.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
