"""The three tray icon pictures: Clawd, greyed Clawd, Clawd with a red dot.

The PNGs in ``assets/icons/`` are built from clawd-tank's own sprite header by
host/tray/make_icons.py; this module only reads them. ``<name>-<size>.png`` is
one square, still picture. The names are those in ``state.ICON_NAMES``.
"""

import ctypes
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image

from .state import ICON_NAMES

ASSETS = Path(__file__).resolve().parent / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64)
ICO_NAME = "clawd.ico"

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
    process must be DPI-aware for this to be more than 16 (see winutil.py)."""
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
    """Loads and caches the pictures. The same PIL image object is returned for
    the same (name, size), so callers can key native handles on it: one handle
    per state."""

    def __init__(self, assets: Optional[Path] = None):
        self.assets = Path(assets) if assets else ASSETS
        self._images: Dict[Tuple[str, int], Image.Image] = {}

    @property
    def ico_path(self) -> Path:
        return self.assets / ICO_NAME

    def icon(self, name: str, size: int) -> Image.Image:
        if name not in ICON_NAMES:
            raise KeyError(f"no tray icon called {name!r}")
        key = (name, size)
        if key not in self._images:
            path = self.assets / "icons" / f"{name}-{size}.png"
            with Image.open(path) as im:
                image = im.convert("RGBA")
            if image.size != (size, size):
                raise ValueError(f"{path.name}: {image.size} is not {size}x{size}")
            self._images[key] = image
        return self._images[key]
