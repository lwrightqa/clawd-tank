"""The Clawd frames the tray icon and the window show.

The PNG strips in ``assets/frames/`` are built from clawd-tank's own sprite
headers by host/tray/make_icons.py; this module only reads PNGs. A strip
``<sprite>-<size>.png`` is N square frames side by side.
"""

import ctypes
import io
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image

ASSETS = Path(__file__).resolve().parent / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64)
ICO_NAME = "clawd.ico"

# Every sprite the app can show. Derived: grey (no bar), error (red dot).
SPRITES = ("idle", "sleeping", "thinking", "typing", "debugger", "building", "conducting",
           "wizard", "beacon", "confused", "sweeping", "happy", "alert", "dizzy",
           "grey", "error")

SM_CXSMICON = 49


def nearest_size(want: int, sizes=SIZES) -> int:
    """The smallest built size that is at least `want`, else the largest."""
    for s in sizes:
        if s >= want:
            return s
    return sizes[-1]


def tray_icon_size() -> int:
    """The notification-area icon size Windows wants at the current DPI, as one of
    the built sizes (16 at 100 %, 20 at 125 %, 24 at 150 %, 32 at 200 %). The
    process must be DPI-aware for this to be more than 16 (see dpi.py)."""
    want = 16
    if sys.platform == "win32":
        try:
            n = ctypes.windll.user32.GetSystemMetrics(SM_CXSMICON)
            if n > 0:
                want = n
        except Exception:
            pass
    return nearest_size(want)


class IconSet:
    """Loads and caches frames. The same PIL image object is returned for the
    same (sprite, frame, size), so callers can key native handles on it."""

    def __init__(self, assets: Optional[Path] = None):
        self.assets = Path(assets) if assets else ASSETS
        self._frames: Dict[Tuple[str, int], List[Image.Image]] = {}
        self._png: Dict[Tuple[str, int, int], bytes] = {}

    @property
    def ico_path(self) -> Path:
        return self.assets / ICO_NAME

    def _strip(self, name: str, size: int) -> List[Image.Image]:
        key = (name, size)
        if key not in self._frames:
            path = self.assets / "frames" / f"{name}-{size}.png"
            with Image.open(path) as im:
                strip = im.convert("RGBA")
            if strip.height != size or strip.width % size:
                raise ValueError(f"{path.name}: {strip.size} is not a strip of {size}px squares")
            self._frames[key] = [strip.crop((i * size, 0, (i + 1) * size, size))
                                 for i in range(strip.width // size)]
        return self._frames[key]

    def frame_count(self, name: str) -> int:
        return len(self._strip(name, SIZES[0]))

    def is_animated(self, name: str) -> bool:
        return self.frame_count(name) > 1

    def frame(self, name: str, index: int, size: int) -> Image.Image:
        frames = self._strip(name, size)
        return frames[index % len(frames)]

    def png(self, name: str, index: int, size: int) -> bytes:
        """The frame as PNG bytes, for tkinter's PhotoImage(data=...)."""
        count = self.frame_count(name)
        key = (name, index % count, size)
        if key not in self._png:
            buf = io.BytesIO()
            self.frame(name, index, size).save(buf, format="PNG")
            self._png[key] = buf.getvalue()
        return self._png[key]
