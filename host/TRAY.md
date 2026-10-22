# Clawd Tank for Windows: the tray app

`ClawdTank.exe` puts Clawd in the notification area and does what the terminal commands used to do. It runs the daemon (the Bluetooth link to the bar and the localhost listener the Claude Code hooks talk to) in the background, installs the hooks into Claude Code when you say yes, and can start with Windows. You never type `python -m clawd_tank_daemon` again.

```
Claude Code hook --> clawd-tank-notify.py --TCP 127.0.0.1:47421--> ClawdTank.exe (daemon inside) --BLE--> the bar
```

The tray icon is Clawd himself, and it shows what he is doing on the bar.

## First run

1. **SmartScreen.** The exe is not code-signed, so Windows shows *Windows protected your PC*. Click **More info**, then **Run anyway**. It asks once per downloaded copy.
2. **Where is the icon?** Windows 11 hides new tray icons in the overflow chevron (the little `^` at the right end of the taskbar). Drag Clawd out of it onto the taskbar to keep him in view. Started by hand, the app also opens its window, so you can tell it is running.
3. **The hooks question.** If Claude Code does not have the hooks yet, a Yes/No dialog appears. It names the file it will edit (`%USERPROFILE%\.claude\settings.json`, or `%CLAUDE_CONFIG_DIR%\settings.json` if you set that), says a backup (`settings.json.bak-YYYYMMDD-HHMMSS`, next to it) is made first, and says all your other settings and hooks are kept. It also names the small helper script it writes (`%USERPROFILE%\.clawd-tank\clawd-tank-notify.py`). Your **Yes** is the consent the command-line installer asks for by typing `yes`. **No** changes nothing and the question is not asked again on later launches; the **Install or repair hooks** button is there when you change your mind. A `settings.json` that is not valid JSON is never touched: the window says so, and you fix the file by hand.
4. **Restart Claude Code** sessions that were already open. Claude Code reads its hooks when it starts.
5. **Close your terminal daemon first.** Only one daemon can hold the bar and port 47421. If one is still running, the icon shows a red dot, the window says *Another Clawd Tank daemon is running; close it.*, and nothing of the other daemon is touched. Close it, then click **Retry**.

The hooks are the ones the command-line installer writes: each runs `py -3 "C:/Users/<you>/.clawd-tank/clawd-tank-notify.py"`. They need the **`py` launcher** (it comes with Python from python.org). If it is missing the window says so.

## The icon

| What Clawd shows | When |
| --- | --- |
| Idle | the bar is connected, a Claude Code session is open and waiting |
| Asleep (Zzz) | the bar is connected and no Claude Code session is open |
| Thinking, typing, reading, running a command, subagents, web search, using a tool, confused, compacting, error, "needs you" | Claude Code is doing that: the same animation the bar plays |
| Greyed (desaturated, see-through) | looking for the bar, or the link dropped (it reconnects by itself) |
| Clawd with a **red dot** | not running: another daemon holds the port, or the engine died (it restarts itself; **Retry** forces it) |

With several sessions the icon shows the most important one: needs you, then error, then the busiest. Animated states change frame about three times a second; nothing runs while the picture is still. The tooltip says the same in words.

**Click** the icon (or double-click) to show the window. **Right-click** for the menu:

| Item | |
| --- | --- |
| **Open** | the window |
| status line (greyed) | bar connected / scanning, what Clawd is doing |
| **Retry** | only while the red dot is showing |
| **Start with Windows** | a checkbox; it reads the registry every time the menu opens |
| **Install or repair hooks** | asks first, as above |
| **Remove hooks** | asks first; makes a backup, removes only Clawd Tank's entries |
| **Open log** | `tray.log` in your default viewer |
| **Quit** | stops everything and clears the bar |

## The window

360 x 300: Clawd at 3x on the left; on the right **Bar** (Connected / Scanning... / Disconnected), **Hooks** (Installed / Not installed / Outdated), **Claude Code** (how many sessions, and the last event with its time). Buttons: **Install or repair hooks**, **Open log**, **Hide to tray**, **Quit**; **Retry** appears next to a red message when the engine is stopped.

**Minimising** hides the window to the tray (no taskbar button) and so does the **X** button. Only **Quit** (button or tray menu) exits the app. Quitting runs the daemon's own shutdown: it sends the bar the "clear" (the bar goes to *No connection*), then frees the port.

## Start with Windows

Tick **Start with Windows** in the tray menu. It writes the value `ClawdTank` under `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` (per user, no administrator rights), pointing at `ClawdTank.exe --minimized`, so Clawd starts in the tray with no window. Untick it to delete the value. If you move the folder, start the exe from its new place once: the entry is repointed.

## Files, log, settings

| What | Where |
| --- | --- |
| The log | `%LOCALAPPDATA%\ClawdTank\tray.log` (1 MB each, 3 older files kept as `tray.log.1` ... `.3`); it also carries the daemon's own lines (`Socket msg`, `Display: anims=[...]`, `Connected to Clawd Tank`) |
| The app's state | `%LOCALAPPDATA%\ClawdTank\state.json` (only: you said No to the hooks question) |
| The daemon's state | `%USERPROFILE%\.clawd-tank\` (`daemon.pid`, `daemon.lock`, `sessions.json`, the notify script), as with the terminal daemon |
| Hooks | `%USERPROFILE%\.claude\settings.json` |

Unexpected errors go to the log and show one message box. The exe has no console window.

The daemon's settings are the same as in [WINDOWS.md](WINDOWS.md): `CLAWD_TANK_TCP_PORT` (default 47421; the hooks need the same value, so set it for the whole account) and `CLAWD_TANK_TRANSPORT`.

Developer switches, never needed day to day: `--no-ble` or `CLAWD_TANK_NO_BLE=1` (no Bluetooth: the bar is never touched), `--no-prompt` or `CLAWD_TANK_NO_PROMPT=1` (no dialog opens by itself), `--minimized` (start in the tray), `--tcp-port N`, `--debug`; `CLAWD_TANK_CLAUDE_DIR` (use another folder's `settings.json`), `CLAWD_TANK_RUN_KEY` (another registry key instead of `Run`), `CLAWD_TANK_INSTANCE` (a separate instance name, so a test never meets your real app). Control commands: `ClawdTank.exe --quit` and `--show` talk to the running instance; `--selftest` imports everything the app needs, without a window and without Bluetooth, and exits 0 if it is all there. A second launch just brings the first one's window forward.

## Build it

Needs Python 3.10+ with the `py` launcher. From the repo:

```powershell
powershell -File host\tray\build-tray.ps1
```

It creates `host\.venv-tray` if it is not there and installs `host\tray\requirements.txt` into it (never into the global Python), rebuilds the icons from the sprite headers (`host\tray\make_icons.py`), and runs PyInstaller on `host\tray\ClawdTank.spec` (onedir, windowed). The result is `host\tray\dist\ClawdTank\ClawdTank.exe` (about 2.5 MB, 36 MB with its folder; keep the folder together). `-Clean` removes `build\` and `dist\` first.

Run it from source instead, with the venv's Python and no exe: `host\.venv-tray\Scripts\python.exe -m clawd_tank_tray` (from `host\`).

**The art.** The sprites are clawd-tank's own (`firmware/main/assets/sprite_*.h`, read-only, RLE RGB565). `make_icons.py` decodes them into PNG strips and `clawd.ico` under `host\clawd_tank_tray\assets\`, so the app never parses a C header. When the headers change (new art), run `py -3 host\tray\make_icons.py` (or the build script, which does) and commit the assets; a test fails if they are stale. `--contact-sheet DIR` also writes sheets of every state at 16/24/32/64 px and of every animation frame. Small sizes are shrunk by block, taking the most common colour of each block, so the flat orange stays crisp and the eyes survive.

Tests (from `host\`, with the venv): `.venv-tray\Scripts\python.exe -m pytest`. The tray tests are `tests\test_tray_*.py` and `tests\test_daemon_embedded.py`. They never touch your bar, your `settings.json`, the real `Run` key or port 47421: the engine tests run the daemon without Bluetooth on a free port; `test_tray_smoke.py` starts the app (from source, and the exe if it is built) as a real process, with `CLAWD_TANK_NO_BLE=1`, a free port, and `USERPROFILE`, `LOCALAPPDATA` and the Claude folder in a temp directory. A tray icon appears for a few seconds while it runs.

## Uninstall

1. Right-click the icon: **Remove hooks** (Yes), then untick **Start with Windows**, then **Quit**.
2. Delete the folder that holds `ClawdTank.exe`.
3. Optional leftovers: `%LOCALAPPDATA%\ClawdTank` (log, state) and `%USERPROFILE%\.clawd-tank` (helper script, daemon state). The backups `settings.json.bak-*` next to your `settings.json` are yours to keep or delete.

## Credits

The Clawd sprites are from clawd-tank, MIT, Copyright (c) 2026 Marcio Granzotto Rodrigues. The licence text is kept in [tray/LICENSE-clawd-tank.txt](tray/LICENSE-clawd-tank.txt). The tray icon uses [pystray](https://github.com/moses-palmer/pystray) and [Pillow](https://python-pillow.org); Bluetooth is [bleak](https://github.com/hbldh/bleak).

## Known limits

- The exe is unsigned (SmartScreen, above), and the first launch of a fresh build can take longer than later ones while Windows checks it.
- Ending it any way but **Quit** (signing out, shutting down, killing it in Task Manager) skips the daemon's shutdown, so the bar is not sent the "clear" and may show Clawd's last state until its Bluetooth link drops.
- With the thinking bubble and a few other pale props, the icon is outlined in grey so it shows on a light taskbar; on a dark one the outline is barely visible.
