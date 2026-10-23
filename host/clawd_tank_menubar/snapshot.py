# host/clawd_tank_menubar/snapshot.py
"""Render the flyout to PNG files, with no window on screen.

    python -m clawd_tank_menubar.snapshot OUTDIR

writes OUTDIR/flyout-<connected|scanning|conflict>-<light|dark>.png. The flyout is
drawn over a solid rounded backdrop that approximates the popover's material, so a
picture can be looked at (and kept as a CI artifact) without opening the real
popover. The sample content is plain data and the AppKit imports are lazy, so this
module imports on every platform; only ``render_png`` needs a Mac.
"""

import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from clawd_tank_tray.hookctl import HookStatus
from clawd_tank_tray.state import EngineStatus, ViewState

from .flyout_model import FlyoutContent, build_content

STATES = ("connected", "scanning", "conflict")
USAGE = "usage: python -m clawd_tank_menubar.snapshot OUTDIR"

# The backdrop: close to the popover's material in each appearance (sRGB 0..255).
BACKDROP = {
    False: {"fill": (236, 236, 236), "border": (208, 208, 208)},
    True: {"fill": (43, 43, 43), "border": (74, 74, 74)},
}
CORNER_RADIUS = 10.0

# A fixed time, so the "Last event" line reads the same in every picture.
SAMPLE_TIME = time.mktime((2026, 1, 15, 14, 32, 7, 0, 0, -1))


def png_name(state: str, dark: bool) -> str:
    return f"flyout-{state}-{'dark' if dark else 'light'}.png"


def _hooks(installed: bool) -> HookStatus:
    return HookStatus(Path("settings.json"), Path("clawd-tank-notify.py"), hooks_present=installed,
                      script_present=installed, script_current=installed)


def sample_contents() -> List[Tuple[str, FlyoutContent]]:
    """(state name, content) for the three pictures, in the order of ``STATES``."""
    connected = ViewState(
        engine=EngineStatus.RUNNING, bar_connected=True,
        display={"anims": ["idle", "typing"], "ids": [1, 2]},
        last_event="PreToolUse Bash in clawd-tank", last_event_time=SAMPLE_TIME)
    scanning = ViewState(engine=EngineStatus.RUNNING)
    # A long event, so the Last event line wraps in the picture.
    conflict = ViewState(
        engine=EngineStatus.CONFLICT,
        last_event="PostToolUse Edit in a-project-with-a-rather-long-name", last_event_time=SAMPLE_TIME)
    return [
        ("connected", build_content(connected, _hooks(True), login_enabled=True)),
        ("scanning", build_content(scanning, _hooks(False), login_enabled=False)),
        ("conflict", build_content(conflict, _hooks(True), login_enabled=False)),
    ]


# --- drawing (macOS only) ---------------------------------------------------------------------

_backdrop_class = None


def _backdrop():
    """The backdrop view class, made on first use (it needs AppKit)."""
    global _backdrop_class
    if _backdrop_class is None:
        import AppKit

        def srgb(rgb):
            return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
                rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, 1.0)

        class ClawdFlyoutSnapshotBackdrop(AppKit.NSView):
            """Clears to transparent, then a filled rounded rectangle with a 1 px border."""

            def drawRect_(self, rect):
                bounds = self.bounds()
                # The bitmap may start with anything in it: clear the corners.
                clear = getattr(AppKit, "NSCompositingOperationClear", 5)
                AppKit.NSRectFillUsingOperation(bounds, clear)
                colors = BACKDROP[bool(getattr(self, "ct_dark", False))]
                path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    AppKit.NSInsetRect(bounds, 0.5, 0.5), CORNER_RADIUS, CORNER_RADIUS)
                srgb(colors["fill"]).setFill()
                path.fill()
                srgb(colors["border"]).setStroke()
                path.setLineWidth_(1.0)
                path.stroke()

        _backdrop_class = ClawdFlyoutSnapshotBackdrop
    return _backdrop_class


def _as_current_appearance(appearance, draw) -> None:
    """Run ``draw`` with ``appearance`` as the current drawing appearance, so dynamic
    colours (labelColor and the rest) resolve for light or dark."""
    import AppKit
    perform = getattr(appearance, "performAsCurrentDrawingAppearance_", None)   # macOS 11 and later
    if perform is not None:
        ran = []

        def run():
            ran.append(True)
            draw()

        try:
            perform(run)                 # PyObjC passes the function as a block
            return
        except Exception:                # noqa: BLE001
            if ran:
                raise                    # the drawing itself failed: do not draw twice
    previous = AppKit.NSAppearance.currentAppearance()
    AppKit.NSAppearance.setCurrentAppearance_(appearance)
    try:
        draw()
    finally:
        AppKit.NSAppearance.setCurrentAppearance_(previous)


def render_png(content: FlyoutContent, path, dark: bool = False, scale: float = 2.0) -> None:
    """Draw the flyout for ``content`` into a PNG at ``path``: its size in points times
    ``scale`` in pixels, light or dark."""
    import AppKit
    from .flyout import build_flyout_view

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)    # no Dock icon

    view = build_flyout_view(content, lambda row_id: None)
    size = view.frame().size
    canvas = _backdrop().alloc().initWithFrame_(AppKit.NSMakeRect(0.0, 0.0, size.width, size.height))
    canvas.ct_dark = bool(dark)
    canvas.addSubview_(view)
    name = AppKit.NSAppearanceNameDarkAqua if dark else AppKit.NSAppearanceNameAqua
    appearance = AppKit.NSAppearance.appearanceNamed_(name)
    canvas.setAppearance_(appearance)        # the subviews inherit it

    # An RGBA bitmap of the wanted pixel size whose size in points is the view's: the
    # drawing is scaled to fill it, which is how a Retina screen draws.
    pixels_w = int(round(size.width * scale))
    pixels_h = int(round(size.height * scale))
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, pixels_w, pixels_h, 8, 4, True, False, AppKit.NSCalibratedRGBColorSpace, 0, 0)
    rep.setSize_(size)
    _as_current_appearance(
        appearance, lambda: canvas.cacheDisplayInRect_toBitmapImageRep_(canvas.bounds(), rep))

    png_type = getattr(AppKit, "NSBitmapImageFileTypePNG", getattr(AppKit, "NSPNGFileType", 4))
    data = rep.representationUsingType_properties_(png_type, {})
    if data is None or not data.writeToFile_atomically_(str(path), True):
        raise OSError(f"could not write {path}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    out = Path(args[0])
    out.mkdir(parents=True, exist_ok=True)
    for state, content in sample_contents():
        for dark in (False, True):
            name = png_name(state, dark)
            render_png(content, out / name, dark=dark)
            print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
