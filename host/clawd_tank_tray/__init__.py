"""Clawd Tank for Windows: a tray app that runs the daemon and installs the hooks.

``python -m clawd_tank_tray`` (or the packaged ``ClawdTank.exe``) puts Clawd in
the notification area. Behind the icon it runs the existing
``clawd_tank_daemon`` in a background thread (the BLE link to the bar and the
localhost hook listener), so nobody has to start a daemon in a terminal.

Module map (see host/TRAY.md for the user's side):

    __main__.py         command line, single instance, logging, starts the app
    app.py              TrayApp: ties the tray icon, the flyout and the engine together
    engine.py           DaemonEngine: the daemon in a thread, restart, stop, port-in-use
    state.py            ViewState: daemon events -> icon state, status lines (pure, no GUI)
    icons.py            the three tray pictures (built by host/tray/make_icons.py)
    tray_icon.py        the pystray icon: one native HICON per state, menu refresh on open
    window.py           the flyout: a borderless tkinter popup with menu rows (see flyout.py, theme.py)
    flyout.py           where the flyout goes, and when it opens and closes (pure logic)
    theme.py            Windows light/dark flyout colours and fonts (pure logic)
    hookctl.py          Claude Code hooks: status, consent text, install, remove
    autostart.py        "Start with Windows" (HKCU Run key)
    single_instance.py  named mutex + named events (show, quit)
    logsetup.py         rotating log, uncaught exceptions
"""

__version__ = "1.0.0"
APP_NAME = "Clawd Tank"
