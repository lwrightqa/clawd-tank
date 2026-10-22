# Clawd Tank host on Windows

The Python host (`host/`) runs on Windows without the macOS menu-bar app. Claude Code's hooks reach a background **daemon**, and the daemon drives the bar over Bluetooth LE. Tested with Windows 11, Python 3.10 and bleak 3.0.2 (its WinRT backend).

**Most people want the tray app instead: [TRAY.md](TRAY.md).** `ClawdTank.exe` runs this same daemon in the background, puts Clawd in the notification area as its icon, installs the hooks after a Yes/No dialog, and can start with Windows, so no terminal and no commands are needed. Everything below is the command-line way, which stays supported (and is what the tray app is built on).

```
Claude Code hook --> clawd-tank-notify.py --TCP 127.0.0.1:47421--> daemon --BLE--> the bar ("Clawd Tank")
```

Upstream uses a Unix socket between the hook and the daemon. Windows has none, so on Windows the same newline-delimited JSON goes over TCP on `127.0.0.1` (loopback only, never the network). macOS and Linux keep the Unix socket by default.

## 1. Set up

Use the `py` launcher: `python` and `python3` on PATH are often Microsoft Store stubs.

```powershell
cd <your clone>/host
py -3 -m venv .venv-host
.\.venv-host\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` skips `rumps` on Windows (it is macOS only), so this installs just `bleak`. For the tests add `pip install -r requirements-dev.txt`.

## 2. Run the daemon

From the same folder, with the venv active:

```powershell
python -m clawd_tank_daemon
```

Leave that window open. The daemon has to be running for the hooks to do anything; Ctrl+C stops it cleanly (it clears the bar first). Useful options:

| Option | Meaning |
| --- | --- |
| `--log-file PATH` | also append the log to a file, for example `--log-file $env:USERPROFILE\.clawd-tank\daemon.log` |
| `--debug` | DEBUG logging, including bleak and each liveness probe |
| `--tcp-port N` | listen on another port (the hooks must use the same one, see the table below) |
| `--transport tcp` | the default on Windows; `unix` is not available there |

### What to expect in the log

```
INFO: Clawd Tank daemon starting: win32, Python 3.10.11, bleak 3.0.2
INFO: Hooks reach this daemon at tcp://127.0.0.1:47421 (set CLAWD_TANK_TRANSPORT / CLAWD_TANK_TCP_PORT to change)
INFO: Transports: ble. Press Ctrl+C to stop.
INFO: Listening on tcp://127.0.0.1:47421 (TCP, localhost only)
INFO: Scanning for Clawd Tank device...
INFO: Found Clawd Tank: Clawd Tank (80:45:6B:35:D1:1E)
INFO: Connected to Clawd Tank (MTU: 256)
INFO: Transport 'ble' connected
INFO: Synced time: epoch 1792553308, tz UTC+4
INFO: Transport 'ble': protocol version 2
INFO: Replaying 0 active notifications
```

Finding the bar took between 1 and 10 seconds, and connecting about 3 more. `tz UTC+4` is the POSIX spelling of UTC-4 (the sign is inverted by design); it follows your Windows time zone. If the bar is off or out of range you see `Scanning for Clawd Tank device...` once, then `Still scanning for Clawd Tank (60s). Is the bar powered and advertising?` every minute; the daemon keeps trying, so powering the bar on is enough. If Bluetooth is turned off in Windows you see `Bluetooth scan failed: ... Is Bluetooth turned on?` every few seconds until you turn it on.

## 3. Pair the bar

There is no pairing step. The bar's GATT characteristics need no encryption, so Windows connects without a bond, and the daemon found and connected to the bar with no pairing in a test run. Do not add it from Settings > Bluetooth & devices; it is not needed. If it is already in that list from an earlier attempt and connecting starts to fail after you reflash the bar, remove it there and restart the daemon. The daemon asks Windows to rediscover the bar's services on every connect, so a new firmware's characteristics are never hidden by a cache.

The bar accepts one connection at a time. Close any other app or phone that is connected to it first.

## 4. Install the hooks

Claude Code must be told to run the notify script on its events. Run, in a terminal window (not from a script or a pipe):

```powershell
python -m clawd_tank_daemon.install_hooks
```

It prints the `hooks` block, the settings file it would edit (`%USERPROFILE%\.claude\settings.json`, or `%CLAUDE_CONFIG_DIR%\settings.json` if you set that), and which events would change. Nothing is written until you **type `yes`** at the prompt; anything else cancels. There is deliberately no flag that skips the question. After `yes` it:

1. writes the notify script to `%USERPROFILE%\.clawd-tank\clawd-tank-notify.py`;
2. copies your `settings.json` to `settings.json.bak-YYYYMMDD-HHMMSS` next to it;
3. writes `settings.json` with the hooks merged in. Your other settings and your own hooks are kept, running it again changes nothing, and a `settings.json` that is not valid JSON is refused rather than overwritten.

To look without changing anything: `python -m clawd_tank_daemon.install_hooks --print`.

Each hook runs `py -3 "C:/Users/<you>/.clawd-tank/clawd-tank-notify.py"`. It is a plain command on the `py` launcher, quoted and with forward slashes so it works whether Claude Code hands it to Git Bash, PowerShell or cmd; there is no shebang and no `.sh`. Restart any running Claude Code session afterwards, since hooks are read at startup.

## 5. Check that it works

With the daemon running and connected, open a new Claude Code session and send any prompt. Clawd on the bar changes from asleep or idle to the thinking animation, then to a tool animation (building for `Bash`, typing for `Edit`, and so on) as Claude works, and back to idle when it stops.

The daemon log shows the same thing, one line per hook:

```
INFO: Socket msg: event=dismiss hook=UserPromptSubmit session=3f9c... [myproject] pid=64612
INFO: Display: anims=['thinking'] subagents=0
INFO: Socket msg: event=tool_use hook= session=3f9c... [myproject] tool=Bash pid=64612
INFO: Display: anims=['building'] subagents=0
```

If the bar does not react, see which of these is missing: no `Socket msg` line means the hooks do not reach the daemon; `Socket msg` but no `Display:` change means the event did not change the state; `Display:` lines but no change on the bar means the BLE link is down (look for `Transport 'ble' disconnected`).

## Settings

The daemon and the notify scripts read the same variables, so set them for both (for a permanent change, in Windows' environment variables).

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLAWD_TANK_TRANSPORT` | `auto` | `auto` is `unix` where available and `tcp` on Windows; `tcp` forces TCP on any OS |
| `CLAWD_TANK_TCP_PORT` | `47421` | TCP port on 127.0.0.1 |
| `CLAWD_TANK_SOCKET` | `~/.clawd-tank/sock` | Unix socket path (macOS and Linux) |

State lives in `%USERPROFILE%\.clawd-tank\`: `daemon.pid`, `daemon.lock`, `sessions.json`, and the notify script.

## Not available on Windows

- The **menu-bar app** (`clawd_tank_menubar`: `rumps`, the status icon, brightness slider, transport menus) and **launch at login** through launchd. Only the hook-installer logic in `clawd_tank_menubar/hooks.py` is shared, and it imports nothing macOS-specific. On Windows the equivalent is the tray app ([TRAY.md](TRAY.md): the tray icon, and a *Start with Windows* checkbox); without it, start the daemon yourself, or add your own Task Scheduler entry that runs `<repo>\host\.venv-host\Scripts\python.exe -m clawd_tank_daemon --log-file ...` at logon.
- The **bundled simulator** (`.app`, `pkill`-based cleanup) is macOS only. The daemon's `--sim` options are untested on Windows.
- The **repo script** `host/clawd-tank-notify` (which can also start the daemon on the first hook) works on Windows as `py -3 <path>\clawd-tank-notify`, but it starts the daemon with whichever Python runs it, which usually does not have `bleak`. The installer therefore uses the standalone copy, which never starts the daemon.

## Troubleshooting

- **`Cannot listen on 127.0.0.1:47421`**: another daemon, or another program, has the port. Close the other daemon, or pick a free port for both sides with `CLAWD_TANK_TCP_PORT`. (The tray app shows the same thing as a red dot on Clawd and *Another Clawd Tank daemon is running; close it.*; close the other one and click Retry.)
- **Claude Code feels slower while the daemon is stopped.** A hook that finds nobody listening gives up after 0.3 seconds. (Windows needs about 2 seconds to report a refused loopback connection, which would be felt on every tool call, so the hooks do not wait for it.) Start the daemon, or remove the hooks.
- **Windows Firewall asks about `python.exe`.** It should not: the listener is bound to 127.0.0.1 only, and loopback traffic is not filtered by default. If it does ask, Cancel or deny; nothing outside this computer needs access.
- **Two `python.exe` entries in Task Manager** for one daemon are normal: a venv's `python.exe` on Windows starts the real interpreter as a child.
- **Another user on the same PC** can send messages to the TCP port (the Unix socket was owner-only). The worst they can do is change what Clawd shows.

## Tests

```powershell
python -m pytest
```

The tests that need Unix sockets, macOS `NSBundle` or the POSIX `ps` walk are skipped on Windows with a reason, and have TCP or Windows counterparts (`test_socket_server.py`, `test_notify_tcp.py`, `test_procutil.py`, `test_pid_resolver_windows.py`).
