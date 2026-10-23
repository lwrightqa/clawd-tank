# host/launcher.py — py2app entry point
# Imports the menubar app as a package so relative imports work.
import sys

from clawd_tank_menubar.app import main

sys.exit(main())
