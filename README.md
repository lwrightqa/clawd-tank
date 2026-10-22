<p align="center">
  <img src="assets/app-icon.svg" width="128" height="128" alt="Clawd Tank App Icon">
</p>

<h1 align="center">Clawd Tank</h1>

A tiny desktop aquarium for your Claude Code sessions.

Clawd Tank is a notification display for Claude Code built on a [Waveshare ESP32-C6-LCD-1.47](https://s.click.aliexpress.com/e/_c4PGS55v) (320x172 ST7789). An animated pixel-art crab named Clawd lives on the screen, reacting to your coding session — alerting on new notifications, celebrating when you dismiss them, and sleeping when you're away.

**No hardware? No problem.** The simulator runs natively on macOS and ships bundled inside the Menu Bar app. Download it from [Releases](https://github.com/marciogranzotto/clawd-tank/releases) — no build tools needed.

<p align="center">
  <img src="assets/sim-recordings/clawd-multi-session.gif" alt="Multi-session display" width="480">
  <img src="assets/sim-recordings/clawd-notification.gif" alt="Clawd notification" width="480">
</p>

## How It Works

```
Claude Code hooks --> clawd-tank-notify --> daemon --> BLE --> ESP32-C6 display
                                                  \-> TCP --> Simulator (SDL2)
```

1. **Claude Code hooks** (`SessionStart`, `PreToolUse`, `PreCompact`, `Stop`, `StopFailure`, `Notification`, `UserPromptSubmit`, `SessionEnd`, `SubagentStart`, `SubagentStop`) fire on session events
2. **clawd-tank-notify** (`~/.clawd-tank/clawd-tank-notify`) forwards the event to the daemon via Unix socket
3. The **daemon** tracks per-session state, maps `tool_name` to animations, and sends JSON payloads to connected transports (BLE hardware, TCP simulator)
4. The **firmware** (or simulator) renders Clawd's tool-aware animation + notification cards on the LCD via LVGL

## Components

| Directory | What | Language |
|-----------|------|----------|
| `firmware/` | ESP-IDF firmware (LVGL UI, NimBLE GATT server, SPI display) | C |
| `simulator/` | Native macOS simulator — runs the same firmware code without hardware | C |
| `host/` | Background daemon, Claude Code hook handler, macOS menu bar app | Python |
| `tools/` | Sprite pipeline (SVG to PNG to RLE-compressed RGB565), GIF recorder, BLE debugging | Python |

## Hardware

- **Board**: [Waveshare ESP32-C6-LCD-1.47](https://s.click.aliexpress.com/e/_c4PGS55v)
- **Display**: 1.47" 320x172 ST7789V (SPI), 16-bit RGB565
- **SoC**: ESP32-C6FH8 (RISC-V, single core), 8MB flash, 512KB SRAM (no PSRAM)
- **RGB LED**: Onboard WS2812B on GPIO8 — flashes on incoming notifications
- **Connectivity**: BLE 5.0 (NimBLE, peripheral role)

## MiniBar bar (ESP32-S3-Touch-LCD-3.49 V2) port

The `bar` branch runs Clawd Tank on the Waveshare ESP32-S3-Touch-LCD-3.49 V2 (ESP32-S3R8: 16 MB flash, 8 MB octal PSRAM, a 640 x 172 AXS15231B panel over QSPI, a TCA9554 I/O expander, no WS2812 LED). It is a standalone build of this repo; nothing from the MiniBar project is needed at build time.

What changed (all under `firmware/`):

- `main/display.c`: the ST7789 SPI bring-up is replaced by the AXS15231B QSPI bring-up (SPI3, 40 MHz, mode 3, 32-bit commands). LVGL runs at the native 172 x 640 with one full-frame RGB565 buffer in PSRAM, software-rotated to 640 x 172 landscape in the flush callback and sent in ten 64-line chunks through a 21.5 KB internal DMA buffer. The backlight is the board's inverted PWM on GPIO 42 plus the enable line on the expander; the BLE brightness setting (0-255) is mapped onto it through MiniBar's brightness curve, and nothing lights before the first frame has reached the panel. `CONFIG_CLAWD_LCD_TURN_180` (menuconfig, "Clawd Configuration") turns the picture if it comes up upside down.
- `main/board_exio.c`, `main/board_pins.h`: the expander driver and the power-on sequence (SYS_EN power hold, LCD reset high, backlight enable low), run first in `app_main`.
- `main/board_lv_mem.c`: LVGL's own allocations go to PSRAM (`CONFIG_LV_USE_CUSTOM_MALLOC`).
- `main/layout.h`, `main/ui_manager.c`, `main/scene.c`, `main/notification_ui.c`: the layout is chosen at run time from `lv_display_get_horizontal_resolution()`. A 320-wide display (the original board, the simulator) keeps the original layout exactly; anything wider gets the wide layout below. There is no compile-time switch, so the simulator builds the same code.
- `main/rgb_led.c`: a no-op on the device (the board has no LED; the simulator keeps its on-screen LED). `main/button.c`: the BOOT button is GPIO 0.

### The wide layout (640 x 172)

Reference renders are in `docs/bar/`: `mock-wide.png` (quiet state, four sessions) and `mock-notification-wide.png` (a notification's hero moment, three sessions); `mock-centred.png`, `mock-notification-centred.png` and `mock-notification-centred-collapsed.png` show the 320 layout the simulator keeps, drawn inside the bar's frame as the first pass did. The mock's fonts are not Montserrat, so text widths there are approximate.

Quiet (no notifications): the scene is the full 640 px. The sky gradient and the grass span the width, the seven-tuft pattern repeats every 160 px, and there are sixteen stars instead of six (the original six, the same six again 330 px to the right, four more in the gaps; same sizes and twinkle). Sessions get equal lanes, each sprite centred in its lane: one session at x 320; two at 160 and 480; three at 106, 320 and 533; four at 80, 240, 400 and 560 (on 320 the original table, 160 / 107 + 213 / 80 + 160 + 240 / 64 + 128 + 192 + 256, stays). The clock is top-right, 6 px in from the edge at y 4; the sub-agent counter stays top-left at (4, 2); the "+N" overflow badge keeps its rule (five or more sessions) and sits 6 px to the left of the clock.

Notification state: the scene shrinks to 420 px (107 on 320) and the panel takes the remaining 220 px at x 420 (it was 300 px in the first wide version; the reference renders in `docs/bar/` still show 300). Up to three sessions stay visible, in equal lanes of 140 px (centres 70, 210 and 350; two sessions at 105 and 315; one at 210), and walk into those lanes as the scene narrows instead of being snapped; a fourth is hidden and the "xN" badge at the scene's right edge counts the total, as it did for the 320 layout's one visible crab. The panel keeps its counter label at (4, 2) and the featured card at (4, 22), 95 % wide (209 px), with the project name, the message and the badge where they were. The message wraps to the card's content width (two lines in the hero card, one once collapsed, an ellipsis after that) instead of scrolling circularly; "Claude needs your permission to run: npm test" is about 275 px in Montserrat 12 against about 193 px of card, so it wraps to two lines in the hero card and is cut with an ellipsis once collapsed. In the hero moment ("> NEW!") the card grows to 80 px rather than the whole panel, with up to three compact rows (14 px each) beneath it from y 108; the rows follow the card's bottom edge during the 350 ms collapse to the 64 px card, after which the list rebuilds at y 90 with up to five rows, as on 320. Panel fade, scene width animation (400 ms) and card collapse are the same code at both widths; walks are 600 ms as before.
- `sdkconfig.defaults`, `partitions.csv`, `main/idf_component.yml`: ESP32-S3 at 240 MHz, 16 MB flash (QIO, 80 MHz), octal PSRAM at 80 MHz with `malloc()` in PSRAM above 4 KB, USB Serial/JTAG console; the factory app partition grown to 0xFF0000 (the partition names stay, so `config_store` keeps its NVS); LVGL pinned to 9.5.0, `espressif/esp_lcd_axs15231b` 1.0.x added, `espressif/led_strip` dropped.

Build with ESP-IDF 5.4.2 (the component manager fetches LVGL and the panel driver on the first configure):

```bash
cd firmware
idf.py set-target esp32s3
idf.py build
```

Build result on 2026-10-20 (ESP-IDF 5.4.2, LVGL 9.5.0, `esp_lcd_axs15231b` 1.0.1~1, with the wide layout): 0 errors, 0 warnings. `clawd-tank.bin` is 0x1FBDD0 bytes (2,080,208, about 2.0 MB) against the 0xFF0000-byte factory partition, 0xDF4230 bytes (88%) free.

**Not flashed yet.** This port has only been built. The panel init, the orientation and the brightness curve are what MiniBar runs on the same board, but nothing here has been seen on hardware. When flashing, use `idf.py -p <port> flash monitor`; the board's USB-C is the ESP32-S3's USB Serial/JTAG port.

Attribution: the board bring-up (expander driver and power hold, panel reset and init sequence, QSPI settings, chunked flush, backlight PWM and brightness curve, pin map) is taken from the MiniBar project's board layer (`firmware/components/board/`), which ported it from Waveshare's ESP-IDF examples for the V2 board (`10_LVGL_V9_Test`, `11_FactoryProgram`, `07_BATT_PWR_Test`). The AXS15231B panel driver is Espressif's `esp_lcd_axs15231b` component (Apache-2.0), pulled by the component manager, not vendored.

## Quick Start

### Download (no hardware needed)

Grab the latest `.app` from [Releases](https://github.com/marciogranzotto/clawd-tank/releases), unzip, and drag to Applications. The app bundles the simulator — a borderless, resizable window shows Clawd on your desktop, driven by your Claude Code sessions.

On first launch: click the crab icon in the menu bar → **Install Claude Code Hooks**. Restart any running Claude Code sessions.

### Build from source — Simulator

```bash
brew install sdl2 cmake

cd simulator
cmake -B build && cmake --build build

# Interactive mode — opens a borderless, resizable SDL2 window
./build/clawd-tank-sim

# Interactive + TCP listener — daemon can connect and drive it
./build/clawd-tank-sim --listen

# Self-contained binary (no Homebrew SDL2 needed)
cmake -B build-static -DSTATIC_SDL2=ON && cmake --build build-static

# Headless mode — outputs PNG screenshots
./build/clawd-tank-sim --headless \
  --events 'connect; wait 500; notify "clawd-tank" "Waiting for input"; wait 2000; disconnect' \
  --screenshot-dir ./shots/ --screenshot-on-event
```

Interactive keys: `c` connect, `d` disconnect, `n` add notification, `1-8` dismiss, `x` clear, `s` screenshot, `z` sleep, `q` quit. The window is borderless and resizable — drag from center, resize from edges.

When `--listen` is active (default port 19872), the daemon can connect over TCP and drive the simulator with the same JSON protocol used over BLE, enabling the full Claude Code → daemon → display pipeline without hardware.

See [simulator/README.md](simulator/README.md) for full CLI reference and JSON scenario support.

### Firmware

Requires [ESP-IDF 5.3.2](https://docs.espressif.com/projects/esp-idf/en/v5.3.2/esp32c6/get-started/index.html) (bundled in `bsp/esp-idf/`, activated via direnv).

```bash
cd firmware
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

### macOS Menu Bar App

The menu bar app bundles the daemon and simulator with a status bar UI. It manages two independent transports — **BLE** (hardware) and **Simulator** (software) — each with their own submenu for enable/disable and connection status.

```bash
# Run from source
cd host && python -m clawd_tank_menubar

# Build and install (builds static simulator, py2app, bundles binary)
cd host && ./build.sh --install

# Or build manually
cd host && pip install py2app && python setup.py py2app
cp ../simulator/build-static/clawd-tank-sim "dist/Clawd Tank.app/Contents/MacOS/"
open "dist/Clawd Tank.app"
```

**Menu features:**
- BLE and Simulator transport submenus with enable/disable toggles
- Simulator window controls: Show/Hide, Always on Top
- Brightness slider, session timeout picker
- Claude Code hook installer
- Version display (tag or branch+N@sha)
- Launch at Login with stale plist detection

On launch, the app automatically installs a hook handler script to `~/.clawd-tank/clawd-tank-notify`. To connect it to Claude Code, click **"Install Claude Code Hooks"** in the menu bar dropdown — this adds the required hooks to `~/.claude/settings.json`. Restart any running Claude Code sessions for hooks to take effect.

Logs are written to `~/Library/Logs/ClawdTank/clawd-tank.log`.

Pre-built releases are available on the [Releases](https://github.com/marciogranzotto/clawd-tank/releases) page.

### Host Daemon (standalone)

The daemon can also run standalone without the menu bar app:

```bash
cd host
pip install -r requirements.txt

# Run daemon with simulator transport
python -m clawd_tank_daemon --sim

# Run daemon with simulator only (no BLE)
python -m clawd_tank_daemon --sim-only
```

The daemon auto-starts on the first hook event. Logs at `~/.clawd-tank/daemon.log`.

## Features

- **Multi-session display** — up to 4 concurrent Claude Code sessions shown as individual animated Clawd sprites, each with their own working animation. New sessions walk in, exiting sessions burrow away
- **Tool-aware animations** — Clawd shows distinct animations based on which tool Claude is using: debugger (Read/Grep), typing (Edit/Write), building (Bash), wizard (WebSearch), conducting (Agent/subagents), beacon (LSP/MCP)
- **Session tracking** — daemon tracks per-session state with priority-based display resolution, staleness eviction, and subagent lifecycle tracking
- **Session persistence** — session state survives daemon restarts, so relaunching the app immediately shows the correct animation for running sessions
- **Time display** — synced from host over BLE on connect (no WiFi/NTP needed)
- **RGB LED flash** — onboard WS2812B cycles through colors on new notifications
- **RLE sprite compression** — all sprite assets compressed ~14:1 (13MB raw → ~900KB)
- **Bundled simulator** — macOS `.app` ships with the simulator binary, no hardware needed. Borderless resizable window with integer pixel scaling
- **Multi-transport** — daemon supports BLE (hardware) and TCP (simulator) transports simultaneously, independently enable/disable
- **Simulator bridge** — full pipeline works without hardware via `--listen` flag and TCP. Window show/hide/pinned controlled over TCP
- **Static SDL2 build** — `STATIC_SDL2=ON` produces a self-contained binary with zero external dependencies
- **Auto-reconnect** — daemon replays active notifications and display state after reconnect on any transport
- **Config over BLE/TCP** — brightness and session timeout adjustable via config characteristic or TCP
- **macOS menu bar app** — transport submenus with colored status indicators, simulator window controls, brightness slider, session timeout, hook installer, version display, launch-at-login

## Clawd's Moods

### Tool-Aware Animations

Clawd's animation reflects which tool Claude is currently using. Each session gets its own Clawd sprite with a tool-specific animation:

| Animation | Tools | |
|-----------|-------|---|
| **Debugger** | `Read`, `Grep`, `Glob` — searching/inspecting code | ![Debugger](assets/sim-recordings/clawd-debugger.gif) |
| **Typing** | `Edit`, `Write`, `NotebookEdit` — writing code | ![Typing](assets/sim-recordings/clawd-typing.gif) |
| **Building** | `Bash` — running shell commands | ![Building](assets/sim-recordings/clawd-building.gif) |
| **Conducting** | `Agent` / active subagents — orchestrating work | ![Conducting](assets/sim-recordings/clawd-conducting.gif) |
| **Wizard** | `WebSearch`, `WebFetch` — conjuring web knowledge | ![Wizard](assets/sim-recordings/clawd-wizard.gif) |
| **Beacon** | `LSP`, MCP tools (`mcp__*`) — communicating with services | ![Beacon](assets/sim-recordings/clawd-beacon.gif) |

### Session States

| State | When | |
|-------|------|---|
| **Multi-session** | 2+ concurrent sessions, each with individual tool animations | ![Multi-session](assets/sim-recordings/clawd-multi-session.gif) |
| **Thinking** | User submitted a prompt, Claude is reasoning | ![Thinking](assets/sim-recordings/clawd-thinking.gif) |
| **Confused** | Claude has been waiting 60s+ for user input | ![Confused](assets/sim-recordings/clawd-confused.gif) |
| **Sweeping** | Context compaction (PreCompact) — oneshot | ![Sweeping](assets/sim-recordings/clawd-sweeping.gif) |

### HUD Badges

| Badge | When | |
|-------|------|---|
| **Subagent counter** | Active subagents — mini-crab icon with `×N` count (top-left) | ![Subagents](assets/sim-recordings/clawd-hud-subagents.gif) |
| **Overflow (wide)** | 5+ sessions — `+N` badge shows extra sessions beyond the 4 visible (top-right) | ![Overflow wide](assets/sim-recordings/clawd-overflow-wide.gif) |
| **Overflow (narrow)** | Notification panel open — `×N` badge shows total session count (top-right of scene) | ![Overflow narrow](assets/sim-recordings/clawd-overflow-narrow.gif) |

### Notification & Lifecycle

| State | When | |
|-------|------|---|
| **Idle** | Connected, no notifications — Clawd hangs out, full-screen with clock | ![Idle](assets/sim-recordings/clawd-idle.gif) |
| **Alert** | New notification arrives — Clawd shifts left, cards appear, LED flashes | ![Alert](assets/sim-recordings/clawd-notification.gif) |
| **Happy** | Notifications dismissed | ![Happy](assets/sim-recordings/clawd-happy.gif) |
| **Sleeping** | No active sessions — all sessions ended or evicted | ![Sleeping](assets/sim-recordings/clawd-sleeping.gif) |
| **Disconnected** | No BLE connection — "No connection" message | ![Disconnected](assets/sim-recordings/clawd-disconnected.gif) |

## Tests

```bash
# C unit tests (notification store)
cd firmware/test && make test

# Python tests (host daemon + protocol)
cd host && pip install -r requirements-dev.txt && pytest
```

## Sprite Pipeline

Clawd's animations are pixel art generated as animated SVGs, rendered to PNG frame sequences, and converted to RLE-compressed RGB565 C headers:

```bash
# SVG animation → PNG frames (requires Playwright)
python tools/svg2frames.py assets/svg-animations/clawd-working-thinking.svg /tmp/frames/ \
  --fps 8 --duration auto --scale 4

# PNG frames → C header
python tools/png2rgb565.py /tmp/frames/ firmware/main/assets/sprite_thinking.h --name thinking

# Record seamlessly-looping GIFs of all animations (for docs)
python tools/record_gif.py --all assets/captures/
```

## BLE Debugging

```bash
# Interactive BLE tool — connect, send notifications, read config
python tools/ble_interactive.py
```

## License

MIT
