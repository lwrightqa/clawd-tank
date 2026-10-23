# Clawd Tank for Windows: the tray app

`ClawdTank.exe` puts Clawd in the notification area and does what the terminal commands used to do. It runs the daemon (the Bluetooth link to the bar and the localhost listener the Claude Code hooks talk to) in the background, installs the hooks into Claude Code when you say yes, and can start with Windows. You never type `python -m clawd_tank_daemon` again.

```
Claude Code hook --> clawd-tank-notify.py --TCP 127.0.0.1:47421--> ClawdTank.exe (daemon inside) --BLE--> the bar
```

The tray icon is one small, still Clawd. It says whether the bar is connected and nothing else; it never animates.

## First run

1. **SmartScreen.** The exe is not code-signed, so Windows shows *Windows protected your PC*. Click **More info**, then **Run anyway**. It asks once per downloaded copy.
2. **Where is the icon?** Windows 11 hides new tray icons in the overflow chevron (the little `^` at the right end of the taskbar). Drag Clawd out of it onto the taskbar to keep him in view. Started by hand, the app also opens its flyout (below), so you can tell it is running.
3. **The hooks question.** If Claude Code does not have the hooks yet, a Yes/No dialog appears. It names the file it will edit (`%USERPROFILE%\.claude\settings.json`, or `%CLAUDE_CONFIG_DIR%\settings.json` if you set that), says a backup (`settings.json.bak-YYYYMMDD-HHMMSS`, next to it) is made first, and says all your other settings and hooks are kept. It also names the small helper script it writes (`%USERPROFILE%\.clawd-tank\clawd-tank-notify.py`). Your **Yes** is the consent the command-line installer asks for by typing `yes`. **No** changes nothing and the question is not asked again on later launches; the **Install or repair hooks** row of the flyout is there when you change your mind. A `settings.json` that is not valid JSON is never touched: the flyout says so, and you fix the file by hand.
4. **Restart Claude Code** sessions that were already open. Claude Code reads its hooks when it starts.
5. **Close your terminal daemon first.** Only one daemon can hold the bar and port 47421. If one is still running, the icon shows a red dot, the flyout says *Another Clawd Tank daemon is running; close it.* in red, and nothing of the other daemon is touched. Close it, then click **Retry**.

The hooks are the ones the command-line installer writes: each runs `py -3 "C:/Users/<you>/.clawd-tank/clawd-tank-notify.py"`. They need the **`py` launcher** (it comes with Python from python.org). If it is missing the flyout says so.

## The icon

| Icon | When |
| --- | --- |
| The orange **Clawd** | the bar is connected. Whatever Claude Code is doing (or not doing), this is the same picture |
| **Greyed** Clawd (grey, see-through) | looking for the bar, or the link dropped (it reconnects by itself) |
| Clawd with a **red dot** | not running: another daemon holds the port, or the engine died (it restarts itself; **Retry** forces it) |

That is all there is: three still pictures, chosen by the connection alone. Nothing is animated and no timer runs; while the connection does not change, the app sends the notification area nothing at all. What Claude Code is doing (sessions, the last event) is shown as text, in the flyout and in the menu, and never changes the icon. The tooltip says the connection in words (*Connected to Clawd Tank*, *Scanning for the bar...*, the port-in-use message).

**Click** the icon to open the flyout (click it again, click anywhere else, or press Esc to close it). **Right-click** for the menu:

| Item | |
| --- | --- |
| **Open** | the flyout (it always opens it; a click on the icon toggles it) |
| status line (greyed) | *Connected to Clawd Tank*, *Scanning for the bar...*, or what is wrong |
| *Last event: ...* (greyed) | the last Claude Code hook the app received, with its time; hidden until there is one |
| **Retry** | only while the red dot is showing |
| **Start with Windows** | a checkbox; it reads the registry every time the menu opens |
| **Install or repair hooks** | asks first, as above |
| **Remove hooks** | asks first; makes a backup, removes only Clawd Tank's entries |
| **Open log** | `tray.log` in your default viewer |
| **Quit** | stops everything and clears the bar |

## The flyout

A click on the icon opens a small **flyout** next to it, like the ones Windows 11 opens from its own tray icons, instead of a window: no title bar, no minimise or close buttons, and no taskbar button or Alt+Tab entry (it is a tool window). On Windows 11 it has rounded corners, a thin border (`#3F3F3F` in dark mode, `#D1D1D1` in light) and a soft shadow. Windows 10 does not know the rounded-corner setting and ignores it, so there the corners are square, like Windows 10's own flyouts; the border is drawn by the flyout itself and the shadow comes from the same DWM call (the Windows 10 look has not been checked on a Windows 10 machine yet).

**Where.** About 300 px wide at 100 % scaling (it grows with the display scale), as tall as its content. It opens against the taskbar with a 12 px gap (scaled), centred on Clawd's icon and kept inside the work area, on the monitor that holds the icon. The icon's place comes from Windows (`Shell_NotifyIconGetRect`); if Windows cannot say, it goes to the corner of the work area where the notification area is, bottom-right for the usual taskbar at the bottom. A taskbar on the top, left or right edge is handled too: the flyout sits against it. When the content gets taller (a red message appears) it grows away from the taskbar and keeps its place against it.

**Opening and closing.**

| Do this | The flyout |
| --- | --- |
| Click the tray icon | opens, or closes if it is open |
| Click anywhere else, or switch to another window | closes (it closes when it loses focus) |
| Press Esc | closes |
| Double-click the Desktop shortcut for `ClawdTank.exe`, start the exe again, or run `ClawdTank.exe --show` | opens, and never closes one that is open; the second copy hands the foreground to the running one and exits |
| **Open** in the right-click menu | opens |
| Start by hand (without `--minimized`) | opens, so you can tell it is running; `--minimized` (what *Start with Windows* uses) starts it closed |

There is no Hide button: clicking away is how you put it away. Clicking the tray icon while the flyout is open closes it, and that same click does not open it again: the mouse-down on the taskbar takes the focus away first, so the flyout closes itself, and a short guard (0.4 s) recognises the mouse-up on the icon as the same click. The click that opens it cannot close it either (it is ignored for 0.25 s). If the tray icon could not be created at all, the flyout does not close on its own (nothing could bring it back); **Quit** still ends the app.

**Inside.** The top row is the small Clawd (the tray's idle picture, 20 px, still) and *Clawd Tank*, with the round **status dot** next to the title, the same three states as the tray icon: green when connected, grey while scanning or disconnected, red on an error (drawn, not an image). Under it, four lines: **Bar** (Connected / Scanning... / Disconnected), **Hooks** (Installed / Not installed / Outdated), **Claude Code** (how many sessions are open) and **Last event** (with its time; it wraps rather than widen the flyout). When the engine is stopped or another daemon holds the port, a red message appears under them with a **Retry** row. Then the rows: full width, a highlight under the pointer and a hand cursor (menu rows, not buttons; Up, Down and Enter work them too):

| Row | |
| --- | --- |
| **Install or repair hooks** | asks first, as above (the flyout closes, then the question opens) |
| **Remove hooks** | asks first; makes a backup, removes only Clawd Tank's entries |
| **Open log** | `tray.log` in your default viewer |
| **Start with Windows** | a check mark on the right when it is on; read from the registry each time the flyout opens; it stays open when you click it, so you see the change |
| **Quit** | below a separator; stops everything and clears the bar |

**Light and dark.** It follows Windows. Dark mode (`AppsUseLightTheme` = 0 under `HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize`) gives `#2B2B2B` with white text, light mode `#F3F3F3` with near-black text, with Windows 11's flyout greys for the border, the line names and the highlight. It is read again every time the flyout opens, so switching Windows between the two shows the next time you click the icon. The font is Segoe UI Variable (Segoe UI before Windows 11), 9 to 10 pt, scaled with the display. If Windows' *Animation effects* are on, it rises out of the taskbar and fades in over about 120 ms; if they are off it just appears.

**Which sessions count.** Claude Code tells the daemon about a session only through its hooks, so a session that is open but idle says nothing. The terminal daemon drops such a session after 10 minutes; the tray app does not. A session stays for as long as its Claude Code process is running, so the bar shows it (and the flyout counts it) when it connects, however long ago you last typed. A session goes when you end it, when its process exits, or, for one that did not report a process, after 10 minutes of silence. The process id is kept in `sessions.json` across a restart of the app and is checked against the process's creation time, so an id Windows has given to some other program does not keep a ghost session.

**No timers.** Hidden, or open and still, the flyout runs no timer and costs nothing: it is redrawn only when a line changes. The only timers are the ~120 ms opening animation and, only if focus is lost in the first 0.3 s after opening, one look-again after that.

Only **Quit** (the row, or in the tray menu) exits the app. Quitting runs the daemon's own shutdown: it sends the bar the "clear" (the bar goes to *No connection*), then frees the port.

## Start with Windows

Tick **Start with Windows** in the tray menu. It writes the value `ClawdTank` under `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` (per user, no administrator rights), pointing at `ClawdTank.exe --minimized`, so Clawd starts in the tray with no window. Untick it to delete the value. If you move the folder, start the exe from its new place once: the entry is repointed.

## Files, log, settings

| What | Where |
| --- | --- |
| The log | `%LOCALAPPDATA%\ClawdTank\tray.log` (1 MB each, 3 older files kept as `tray.log.1` ... `.3`); it also carries the daemon's own lines (`Socket msg`, `Display: anims=[...]`, `Connected to Clawd Tank`). Windows cannot rename a file another program has open: if Notepad (after **Open log**), an editor or a `tail` still has `tray.log` open when it is full, the app keeps writing into it, notes `Could not rotate ...` in it, and tries again 30 s later, so the file can run a little past 1 MB but never goes quiet |
| The app's state | `%LOCALAPPDATA%\ClawdTank\state.json` (only: you said No to the hooks question) |
| The daemon's state | `%USERPROFILE%\.clawd-tank\` (`daemon.pid`, `daemon.lock`, `sessions.json`, the notify script), as with the terminal daemon |
| Hooks | `%USERPROFILE%\.claude\settings.json` |

Unexpected errors go to the log and show one message box. The exe has no console window.

The daemon's settings are the same as in [WINDOWS.md](WINDOWS.md): `CLAWD_TANK_TCP_PORT` (default 47421; the hooks need the same value, so set it for the whole account) and `CLAWD_TANK_TRANSPORT`.

Developer switches, never needed day to day: `--no-ble` or `CLAWD_TANK_NO_BLE=1` (no Bluetooth: the bar is never touched), `--no-prompt` or `CLAWD_TANK_NO_PROMPT=1` (no dialog opens by itself), `--minimized` (start in the tray), `--tcp-port N`, `--debug`; `CLAWD_TANK_CLAUDE_DIR` (use another folder's `settings.json`), `CLAWD_TANK_RUN_KEY` (another registry key instead of `Run`), `CLAWD_TANK_INSTANCE` (a separate instance name, so a test never meets your real app), `CLAWD_TANK_THEME=light` or `dark` (use that flyout theme whatever Windows says; for screenshots and tests). Control commands: `ClawdTank.exe --quit` and `--show` talk to the running instance; `--selftest` imports everything the app needs, without a window and without Bluetooth, and exits 0 if it is all there. A second launch just opens the first one's flyout.

## Build it

Needs Python 3.10+ with the `py` launcher. From the repo:

```powershell
powershell -File host\tray\build-tray.ps1
```

It creates `host\.venv-tray` if it is not there and installs `host\tray\requirements.txt` into it (never into the global Python), rebuilds the icons from the sprite header (`host\tray\make_icons.py`), and runs PyInstaller on `host\tray\ClawdTank.spec` (onedir, windowed). The result is `host\tray\dist\ClawdTank\ClawdTank.exe` (about 2.5 MB, 36 MB with its folder; keep the folder together). `-Clean` removes this build's output and work folders first.

To build next to a copy that is running, give another output folder: `powershell -File host\tray\build-tray.ps1 -OutDir dist-next` (relative to `host\tray`; the work folder becomes `build-dist-next`, or name it with `-WorkDir`). Nothing outside those two folders is touched, and the script stops before building if the `ClawdTank.exe` it would overwrite is running.

Run it from source instead, with the venv's Python and no exe: `host\.venv-tray\Scripts\python.exe -m clawd_tank_tray` (from `host\`).

**The art.** Clawd is clawd-tank's own sprite (`firmware/main/assets/sprite_idle.h`, read-only, RLE RGB565); only its first frame is used. `make_icons.py` decodes it into the three tray pictures at 16, 20, 24, 32, 40, 48 and 64 px (`assets\icons\idle-<size>.png`, `grey-...`, `error-...`) and into `clawd.ico` (the idle Clawd at those sizes and 256, for the exe and the message boxes; the flyout's 20 px mark is the idle tray picture), under `host\clawd_tank_tray\assets\`, so the app never parses a C header. That is the whole of `assets\` (plus `icons.json`, which records where it came from); the exe carries the icons and the .ico only. When the header changes (new art), run `py -3 host\tray\make_icons.py` (or the build script, which does) and commit the assets; a test fails if they are stale. `--contact-sheet DIR` also writes sheets of the three states at 16/24/32/64 px, on dark and light backgrounds. Small sizes are shrunk by block, taking the most common colour of each block, so the flat orange stays crisp and the eyes survive.

Tests (from `host\`, with the venv): `.venv-tray\Scripts\python.exe -m pytest`. The tray tests are `tests\test_tray_*.py` and `tests\test_daemon_embedded.py`. They never touch your bar, your `settings.json`, the real `Run` key or port 47421: the engine tests run the daemon without Bluetooth on a free port; `test_tray_smoke.py` starts the app (from source, and the exe if it is built) as a real process, with `CLAWD_TANK_NO_BLE=1`, a free port, and `USERPROFILE`, `LOCALAPPDATA` and the Claude folder in a temp directory. A tray icon appears for a few seconds while it runs. The flyout tests use real windows with real handles but make them invisible (transparent, parked off-screen, no focus taken), so nothing shows on the desktop and the foreground window is left alone; the smoke test opens the real flyout for a moment. `CLAWD_TANK_TEST_EXE=<path to ClawdTank.exe>` points the smoke test at another build (for example one in `dist-next`; it is always run); `CLAWD_TANK_TEST_EXE=none` skips the exe run. Without it the smoke test runs `dist\ClawdTank\ClawdTank.exe` only if that is not older than the source (a stale build is skipped with a reason: rebuild it).

## Uninstall

1. Right-click the icon: **Remove hooks** (Yes), then untick **Start with Windows**, then **Quit**.
2. Delete the folder that holds `ClawdTank.exe`.
3. Optional leftovers: `%LOCALAPPDATA%\ClawdTank` (log, state) and `%USERPROFILE%\.clawd-tank` (helper script, daemon state). The backups `settings.json.bak-*` next to your `settings.json` are yours to keep or delete.

## Credits

The Clawd sprites are from clawd-tank, MIT, Copyright (c) 2026 Marcio Granzotto Rodrigues. The licence text is kept in [tray/LICENSE-clawd-tank.txt](tray/LICENSE-clawd-tank.txt). The tray icon uses [pystray](https://github.com/moses-palmer/pystray) and [Pillow](https://python-pillow.org); Bluetooth is [bleak](https://github.com/hbldh/bleak).

## Known limits

- The exe is unsigned (SmartScreen, above), and the first launch of a fresh build can take longer than later ones while Windows checks it.
- Ending it any way but **Quit** (signing out, shutting down, killing it in Task Manager) skips the daemon's shutdown, so the bar is not sent the "clear" and may show Clawd's last state until its Bluetooth link drops.
- The icon shows the connection only. If you want to see that Claude Code is working, open the flyout or the tray menu (*Last event*); the bar itself still plays the animations.
