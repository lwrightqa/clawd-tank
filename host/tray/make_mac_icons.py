#!/usr/bin/env python3
"""Build the macOS menu bar icons from the Windows tray app's Clawd pictures.

The sprite is clawd-tank's (MIT, Copyright (c) 2026 Marcio Granzotto
Rodrigues; see host/tray/LICENSE-clawd-tank.txt). The 64 px pictures in
host/clawd_tank_tray/assets/icons/ were made from it by make_icons.py; this
script only shrinks them, so the Mac and the Windows app show the same Clawd.

    python host/tray/make_mac_icons.py            # rebuild host/clawd_tank_menubar/icons

It writes, for the menu bar (18 pt: 18 px, and 36 px for Retina) and for the
flyout header (20 pt: 20 px and 40 px). Names are the ones clawd_tank_menubar
loads; the @2x file next to a 1x file is the Retina picture:

    clawd-connected.png / @2x     idle-64.png  (Clawd: the bar is connected)
    clawd-disconnected.png / @2x  grey-64.png  (greyed: not connected)
    clawd-error.png / @2x         error-64.png (Clawd with a red dot)
    clawd-header.png / @2x        idle-64.png  (the flyout's title row)

None of them is a template image: the colours are drawn as they are. The old
crab-*.png files in that folder belong to the upstream app and are left alone.
"""

import argparse
import sys
from pathlib import Path
from typing import Dict, Tuple

from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SOURCE_DIR = REPO / "host" / "clawd_tank_tray" / "assets" / "icons"
OUT_DIR = REPO / "host" / "clawd_tank_menubar" / "icons"

MENU_BAR_PT = 18     # the menu bar picture, in points
HEADER_PT = 20       # the flyout's title row picture, in points
SOURCE_SIZE = 64     # which size of the tray pictures to shrink from

# output name -> (tray picture it comes from, size in points)
ICONS: Dict[str, Tuple[str, int]] = {
    "clawd-connected": ("idle", MENU_BAR_PT),
    "clawd-disconnected": ("grey", MENU_BAR_PT),
    "clawd-error": ("error", MENU_BAR_PT),
    "clawd-header": ("idle", HEADER_PT),
}


def shrink(src: Image.Image, px: int) -> Image.Image:
    """src scaled to px by px. Pillow resizes RGBA on premultiplied alpha, so the
    transparent edge does not bleed a dark halo into the picture."""
    return src.convert("RGBA").resize((px, px), Image.LANCZOS)


def file_names(name: str) -> Tuple[str, str]:
    """The 1x and the Retina file name of an icon."""
    return f"{name}.png", f"{name}@2x.png"


def build(source_dir: Path = SOURCE_DIR) -> Dict[str, Image.Image]:
    """file name -> image, for every file this script writes. Nothing is written
    here; write() does that."""
    images: Dict[str, Image.Image] = {}
    for name, (tray_name, points) in ICONS.items():
        src = Image.open(source_dir / f"{tray_name}-{SOURCE_SIZE}.png")
        one_x, two_x = file_names(name)
        images[one_x] = shrink(src, points)
        images[two_x] = shrink(src, points * 2)
    return images


def write(source_dir: Path = SOURCE_DIR, out_dir: Path = OUT_DIR) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for file_name, image in build(source_dir).items():
        path = out_dir / file_name
        image.save(path, optimize=True)
        written[file_name] = path
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", type=Path, default=SOURCE_DIR,
                    help="folder with idle-64.png, grey-64.png, error-64.png "
                         "(default: host/clawd_tank_tray/assets/icons)")
    ap.add_argument("--out", type=Path, default=OUT_DIR,
                    help="output folder (default: host/clawd_tank_menubar/icons)")
    args = ap.parse_args(argv)
    written = write(args.source, args.out)
    for file_name in sorted(written):
        print(f"wrote {written[file_name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
