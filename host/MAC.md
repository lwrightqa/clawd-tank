# Clawd Tank for macOS: the menu bar app

`Clawd Tank.app` puts Clawd in the menu bar and does what the terminal commands used to do. It runs the daemon (the Bluetooth link to the bar and the socket the Claude Code hooks talk to) in the background, installs the hooks into Claude Code when you say yes, and can start at login. A **left click** on Clawd opens a flyout that looks like the one in the Windows tray app ([TRAY.md](TRAY.md)); a **right click** (or a control-click) opens the original menu, with all its original items.

```
Claude Code hook --> ~/.clawd-tank/clawd-tank-notify --Unix socket ~/.clawd-tank/sock--> Clawd Tank.app (daemon inside) --BLE--> the bar
```

The app is the original project's menu bar app (`host/clawd_tank_menubar/`, rumps and py2app) with the flyout added. The flyout reuses the Windows app's logic for the text it shows and for the hooks question, so both say the same things.

**Status: this has not been run on a real Mac, and never with the bar.** It is built, tested and snapshotted by a GitHub Actions workflow on GitHub's Mac runner (macOS 14, Apple silicon), where the built app has started, put Clawd in the menu bar and opened the flyout under it. The list of what that does and does not prove is at the end: [What is not verified](#what-is-not-verified).

## Get the app

There is no release yet. The workflow **macOS flyout** (`.github/workflows/macos-flyout.yml`) builds the app and a disk image on every push to the `mac-flyout` branch that touches `host/`, and on demand:

1. On GitHub, open the repository's **Actions** tab and choose **macOS flyout** in the list on the left, then a run. (**Run workflow** starts a new one.)
2. Scroll to **Artifacts** at the bottom of the run's page and download **Clawd-Tank-macOS**. You need a GitHub account with access to the repository. Artifacts are deleted after the repository's retention period (90 days unless it was changed).
3. The download is a zip with `Clawd-Tank.dmg` in it. Open the dmg and drag **Clawd Tank** onto **Applications**.

A run whose tests failed still uploads everything it built, so check the run's result before you trust the app. The other artifacts of a run:

| Artifact | Holds |
| --- | --- |
| `flyout-snapshots` | PNGs of the flyout drawn by the app's own code: connected, scanning and conflict, each in light and dark |
| `flyout-screen` | a screenshot of the runner's screen with the app running and its flyout open under the Clawd icon, the output of the selftest, and the app's log |
| `test-results` | the host tests' result as JUnit XML |

When this fork publishes a release, its disk image would come from the existing build workflows (`build-macos-app.yml` and `release.yml`, upstream's, unchanged). They have not been run for this fork.

## First run

1. **The app is not signed or notarised**, so macOS refuses to open it at first. Use one of these:
   - macOS 12 to 14: **Control-click** (right-click) the app in Applications, choose **Open**, then **Open** again.
   - macOS 15 and later: Apple removed that route. Try to open the app once, then open **System Settings > Privacy & Security**, scroll to *Security*, and click **Open Anyway** next to the line about Clawd Tank. Confirm with your password or Touch ID.
   - Or from Terminal, which also answers a "damaged and can't be opened" message: `xattr -dr com.apple.quarantine "/Applications/Clawd Tank.app"`
2. **Where is Clawd?** The app has no Dock icon and no window. Look at the right end of the menu bar. On a Mac with a notch, a crowded menu bar can hide icons behind the notch; quit some other menu bar apps if Clawd is not there.
3. **The Bluetooth question.** The first time the app looks for the bar, macOS asks whether Clawd Tank may use Bluetooth (the text is "Clawd Tank uses Bluetooth to communicate with the ESP32 display."). Click **Allow**. If you said Don't Allow, switch Clawd Tank on in **System Settings > Privacy & Security > Bluetooth** and start the app again.
4. **The hooks question.** If Claude Code does not have the hooks yet, a Yes/No dialog appears. It names the file it will edit (`~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json` if you set that), says a backup (`settings.json.bak-YYYYMMDD-HHMMSS`, next to it) is made first, and says all your other settings and hooks are kept. It also names the small helper script it writes (`~/.clawd-tank/clawd-tank-notify`). Your **Yes** is the consent the command-line installer asks for by typing `yes`. **No** changes nothing and the question is not asked again on later launches; the **Install or repair hooks** row of the flyout is there when you change your mind (removing the hooks yourself also stops the question). A `settings.json` that is not valid JSON is never touched: the dialog says so, and you fix the file by hand.
5. **Restart Claude Code** sessions that were already open. Claude Code reads its hooks when it starts.
6. **Close any terminal daemon first**, and any other app or phone connected to the bar. Only one daemon can hold the bar, and the bar takes one connection. If another daemon is running, the icon shows a red dot and the flyout says *Another Clawd Tank daemon is running; close it.* in red. Close it, then click **Retry**.

The hook runs the helper script directly. It starts with `#!/usr/bin/env python3` and uses only Python's standard library, so any Python 3 on the `PATH` will do; the app itself does not need one installed.

## The icon

| Icon | When |
| --- | --- |
| **Clawd** | the bar is connected (also in test mode, where there is no bar to wait for) |
| **Greyed** Clawd | looking for the bar, the link dropped (it reconnects by itself), or BLE is switched off in the menu |
| Clawd with a **red dot** | the daemon is not running: another daemon holds the lock, or it stopped (**Retry** starts it again) |

These are the same three pictures as the Windows tray icon. They are drawn in their own colours (not the menu bar's black-and-white template style), chosen by the connection alone. What Claude Code is doing is text in the flyout, never a different icon.

**Left click** opens the flyout; click the icon again, or anywhere else, to close it. **Right click**, or **control-click**, opens the original menu:

| Item | |
| --- | --- |
| **BLE** | a submenu: the link's status, an **Enabled** switch, **Reconnect** |
| **Simulator** | a submenu: status, **Enabled**, **Show Window**, **Always on Top**. This build does not bundle the simulator, so the menu says *Not available* and there is nothing to start |
| **Brightness** | a slider, 0 to 255, sent to the bar over Bluetooth |
| **Session Timeout** | 1, 2, 5, 10 or 30 minutes, or **Never** (see [Which sessions count](#which-sessions-count)) |
| **Install Claude Code Hooks** | the same as the flyout's **Install or repair hooks** row: it asks first |
| **Launch at Login** | the same as the flyout's **Start at login** row |
| *Version: ...* (greyed) | which build this is |
| **Quit Clawd Tank** | stops the daemon and the app |

## The flyout

About 300 pt wide and as tall as its content, opening under the icon. The top row is Clawd and **Clawd Tank** in bold, with a round **status dot** next to it: green when connected, grey while scanning or disconnected, red on an error. It follows macOS, light and dark mode alike. Under the title, four lines:

| Line | Says |
| --- | --- |
| **Bar** | *Connected*, *Scanning...*, *Disconnected*, *Bluetooth off* (you switched BLE off in the menu), or *Bluetooth off (test mode)* |
| **Hooks** | *Installed*, *Not installed*, an *Outdated* or *Not working* note about the helper script, or *settings.json is not valid JSON* |
| **Claude Code** | *no active session*, or how many sessions are open |
| **Last event** | the last Claude Code hook the app received, with its time, such as `PreToolUse Bash in myproject, 14:32:07`; it wraps rather than widen the flyout. *no event yet* until one arrives |

When something is wrong, a red message appears under the lines with a **Retry** row. Then the rows, full width, with a highlight under the pointer:

| Row | |
| --- | --- |
| **Install or repair hooks** | asks first, as above (the flyout closes, then the question opens) |
| **Remove hooks** | asks first; makes a backup, removes only Clawd Tank's entries |
| **Open log** | `clawd-tank.log` in the app macOS uses for log files (usually Console) |
| **Start at login** | a check mark at the right when it is on; the flyout stays open when you click it, so you see the change |
| **Quit** | below a separator; stops the daemon through its normal shutdown, so the bar is sent "clear" and shows *No connection* |

### Which sessions count

Claude Code tells the daemon about a session only through its hooks, so a session that is open but idle says nothing. The terminal daemon drops such a session after 10 minutes of silence; this app does not. A session stays for as long as its Claude Code process is running, so the bar shows it (and the flyout counts it) when the app starts, however long ago you last typed. A session goes when you end it, when its process exits, or, for one that did not report a process, after the **Session Timeout** of silence (**Never** means no limit). The process id is kept in `~/.clawd-tank/sessions.json` across a restart of the app and is checked against the process's start time, so a recycled id does not keep a ghost session.

## Start at login

Tick **Start at login** in the flyout (or **Launch at Login** in the menu). It writes a launchd user agent, `~/Library/LaunchAgents/com.clawd-tank.menubar.plist` (per user, no administrator rights), that runs the app's own executable when you log in. It takes effect at your next login: ticking it does not start a second copy now. Untick it to delete the file (and to unload the agent if an older version had loaded it); unticking from a copy that the agent itself started at login leaves that copy running. If you move the app, start it once from its new place: the agent is repointed. macOS 13 and later may list it under **System Settings > General > Login Items & Extensions > Allow in the Background**; if you switch it off there, it will not start.

## Files, log, settings

| What | Where |
| --- | --- |
| The log | `~/Library/Logs/ClawdTank/clawd-tank.log`; it carries the daemon's own lines (`Socket msg`, `Display: anims=[...]`, `Connected to Clawd Tank`). The flyout's **Open log** row opens it |
| The app's preferences | `~/.clawd-tank/preferences.json` (BLE and simulator switches, and whether you said No to the hooks question) |
| The daemon's state | `~/.clawd-tank/` (`daemon.pid`, `daemon.lock`, `sessions.json`, `sock`, the helper script `clawd-tank-notify`), as with the terminal daemon |
| Hooks | `~/.claude/settings.json` |
| The login agent | `~/Library/LaunchAgents/com.clawd-tank.menubar.plist` |

The daemon's own settings are the same as in the README: `CLAWD_TANK_SOCKET` (the Unix socket the hooks use; set it for the whole account) and `CLAWD_TANK_TRANSPORT`.

## Test mode

Developer switches, never needed day to day. An app started from Finder does not see variables set in a shell, so start it from Terminal:

```bash
CLAWD_TANK_NO_BLE=1 CLAWD_TANK_NO_PROMPT=1 "/Applications/Clawd Tank.app/Contents/MacOS/Clawd Tank"
```

| Setting | Effect |
| --- | --- |
| `CLAWD_TANK_NO_BLE=1` | no Bluetooth and no simulator: the bar is never touched. The Bar line says *Bluetooth off (test mode)* and the icon shows Clawd as connected |
| `CLAWD_TANK_NO_PROMPT=1` | no dialog opens by itself (the first-run hooks question) |
| `CLAWD_TANK_OPEN_FLYOUT=1` | opens the flyout about 2 seconds after launch (the workflow uses it to take its screenshot) |
| `CLAWD_TANK_CLAUDE_DIR=<folder>` | use that folder's `settings.json` instead of `~/.claude` |
| `--selftest` | checks the imports and builds the flyout for some sample content, with no Bluetooth, no window and no popover; prints `selftest ok` and exits 0, or says what failed and exits 1 |

From source on a Mac (Python 3.10 or newer; `rumps` pulls in PyObjC): `cd host && pip install -r requirements.txt && python -m clawd_tank_menubar`, with the same switches. `python -m clawd_tank_menubar.snapshot OUTDIR` draws the flyout (connected, scanning and conflict, light and dark) into `OUTDIR` as `flyout-<state>-<light|dark>.png`; that is what the workflow's `flyout-snapshots` artifact holds.

## Build it

The workflow does this on `macos-14` with Python 3.11, from `host/`:

```bash
pip install -r requirements.txt py2app pytest pytest-asyncio pillow
python -m pytest tests                 # without the Windows-only tray test modules
python tray/make_mac_appicon.py        # Clawd's app icon, replacing the committed crab
python setup.py py2app                 # dist/Clawd Tank.app
```

then runs the built app's executable with `--selftest`, launches it once in test mode with a screen capture, and makes the disk image the way `build-macos-app.yml` does. These are also the commands to build it on your own Mac, in a virtual environment. Four notes:

- The six tray test modules the workflow leaves out (`test_tray_app`, `_autostart`, `_icon`, `_integration`, `_smoke` and `_window`) test the Windows app and cannot even be imported on a Mac. The other Windows-only tray tests skip themselves with a reason.
- `host/AppIcon.icns` in git is upstream's crab. `make_mac_appicon.py` overwrites it (it enlarges `clawd_tank_tray/assets/icons/idle-64.png` without blending onto a 1024 px canvas and runs `iconutil`); undo it with `git checkout host/AppIcon.icns` if you do not want to commit the new icon. It needs macOS, but `--no-icns --iconset DIR` writes just the PNGs on any system.
- `setup.py` bakes the git version into the app (the *Version* line of the menu). `CLAWD_TANK_VERSION` replaces it; the workflow sets it so a build does not read `-dirty`.
- There is no simulator in this build: no cmake, no LVGL, no SDL2. `host/build.sh` (upstream's) still builds one and bundles it, if you want that.

py2app bundles `clawd_tank_daemon` and `clawd_tank_menubar` whole, plus only three modules of the Windows tray app (`clawd_tank_tray`, `.state` and `.hookctl`, which are plain logic); the tray's tkinter, pystray and Win32 modules are left out.

## Uninstall

1. In the flyout: **Remove hooks** (Yes), untick **Start at login**, then **Quit**.
2. Delete **Clawd Tank** from Applications.
3. Optional leftovers: `~/Library/Logs/ClawdTank` (log), `~/.clawd-tank` (helper script, daemon state, preferences). The backups `settings.json.bak-*` next to your `settings.json` are yours to keep or delete.

## What is not verified

- **No real Mac, no bar.** Nothing here has been run on a Mac by hand, and never with the bar. The Bluetooth link, the brightness slider and the whole hook-to-bar path on macOS are the original project's code paths, which have not been run on this board.
- **What the Mac runner proves.** The workflow runs the host tests (the whole suite passes on macOS), draws the flyout, builds the bundle, runs the built app's selftest and starts it in test mode (no Bluetooth). In the last runs the app stayed up, its status item sat on the menu bar and the flyout opened under it. The tests use fakes for Bluetooth; the AppKit code is exercised only by that runner.
- **Apple silicon only.** The runner is `macos-14` (Apple silicon). The app is built there and has not been built or tried for Intel Macs.
- **macOS versions.** The code is written for macOS 12 and later, but the only version it has run on is the runner's, 14. Not tried on 12, 13 or 15.
- **Not notarised.** There is no Developer ID signature. Whether py2app's own ad-hoc signature is on the bundle is printed in the workflow's *Inspect the bundle* step, and this guide does not assume it. Expect the Gatekeeper steps above on every machine and every new download.
- **The simulator is not bundled.** The Simulator menu has nothing to start.
- **The flyout's look.** The snapshots are the real view drawn off screen by the runner, and the runner's screenshot shows the real popover under the icon in light mode. Nothing has been compared with a real menu bar on a Mac in use, a notch, a second display, dark mode on screen, or the Windows flyout side by side.
- **Not seen by hand:** a real click on the icon, the Bluetooth permission dialog (its wording is from the app's `Info.plist`), the hooks dialog on a Mac, whether the keyboard goes back to the app you were typing in when the flyout closes (the code does it; no test can watch focus), switching between light and dark while the flyout is open, a control-click, and **Start at login** in the built app (the launchd agent).
- **The helper script needs a `python3`** on the `PATH` when Claude Code runs a hook. On a Mac without one, macOS may offer to install the Command Line Tools the first time a hook runs; that has not been tried.

## Credits

The Clawd pictures are made from clawd-tank's sprite, MIT, Copyright (c) 2026 Marcio Granzotto Rodrigues; the licence text is kept in [tray/LICENSE-clawd-tank.txt](tray/LICENSE-clawd-tank.txt). The menu bar app is [rumps](https://github.com/jaredks/rumps) on PyObjC, packaged with [py2app](https://py2app.readthedocs.io); Bluetooth is [bleak](https://github.com/hbldh/bleak); the app icon is built with [Pillow](https://python-pillow.org).
