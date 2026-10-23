#!/usr/bin/env python3
"""Build the macOS app icon (AppIcon.icns) from the tray app's Clawd.

The committed host/AppIcon.icns is upstream's crab. The macOS workflow
(.github/workflows/macos-flyout.yml) runs this script in the CI workspace to
replace it with Clawd, so the build does not need the file in git to change.

    python host/tray/make_mac_appicon.py                    # writes host/AppIcon.icns (macOS: needs iconutil)
    python host/tray/make_mac_appicon.py --iconset DIR      # also keeps the .iconset folder
    python host/tray/make_mac_appicon.py --no-icns --iconset DIR   # PNGs only, on any OS

The picture is host/clawd_tank_tray/assets/icons/idle-64.png (made by
host/tray/make_icons.py from clawd-tank's sprite, MIT; see
host/tray/LICENSE-clawd-tank.txt). It is cropped to Clawd, enlarged by a whole
number with nearest-neighbour sampling (no blending, so the pixel art stays
sharp) and centred on a transparent 1024 px canvas with padding. The ten sizes
of the .iconset are made from that master by area averaging; iconutil turns the
folder into the .icns.
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

from PIL import Image

HERE = Path(__file__).resolve().parent
HOST = HERE.parent
SOURCE = HOST / "clawd_tank_tray" / "assets" / "icons" / "idle-64.png"
DEFAULT_OUT = HOST / "AppIcon.icns"

CANVAS = 1024      # the largest picture in an .iconset (512 pt at 2x)
PADDING = 96       # transparent border on every side of the master, in canvas pixels

# (point size, scale): the ten files iconutil expects, named icon_<n>x<n>[@2x].png.
ICONSET_SIZES = ((16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
                 (256, 1), (256, 2), (512, 1), (512, 2))


def build_master(source: Path = SOURCE) -> Image.Image:
    """The 1024 x 1024 RGBA master: Clawd, enlarged by a whole number, centred."""
    src = Image.open(source).convert("RGBA")
    box = src.getchannel("A").getbbox()
    if box is None:
        raise ValueError(f"{source} has no visible pixels")
    src = src.crop(box)
    factor = max(1, (CANVAS - 2 * PADDING) // max(src.size))
    big = src.resize((src.width * factor, src.height * factor), Image.Resampling.NEAREST)
    master = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    master.alpha_composite(big, ((CANVAS - big.width) // 2, (CANVAS - big.height) // 2))
    return master


def iconset_name(points: int, scale: int) -> str:
    return f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"


def write_iconset(master: Image.Image, folder: Path) -> List[Path]:
    """Write the ten PNGs into ``folder`` (created if needed) and return their paths."""
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for points, scale in ICONSET_SIZES:
        px = points * scale
        # BOX averages with premultiplied alpha; the master is already pixel art at
        # 13x, so a shrink by area keeps the eyes where they are.
        pic = master.copy() if px == master.width else master.resize(
            (px, px), Image.Resampling.BOX)
        path = folder / iconset_name(points, scale)
        pic.save(path, "PNG")
        written.append(path)
    return written


def make_icns(iconset: Path, out: Path) -> None:
    """Run iconutil (macOS only) on an .iconset folder."""
    if shutil.which("iconutil") is None:
        raise RuntimeError("iconutil was not found: making an .icns needs macOS "
                           "(use --no-icns --iconset DIR to write only the PNGs)")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build AppIcon.icns from the tray app's Clawd.")
    ap.add_argument("--source", type=Path, default=SOURCE, help="the picture to enlarge")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="the .icns to write")
    ap.add_argument("--iconset", type=Path, default=None,
                    help="keep the .iconset folder here (default: a temporary one)")
    ap.add_argument("--no-icns", action="store_true",
                    help="write the .iconset only, without iconutil (needs --iconset)")
    args = ap.parse_args(argv)
    if args.no_icns and args.iconset is None:
        ap.error("--no-icns needs --iconset DIR")
    if args.iconset is not None and not args.no_icns and args.iconset.suffix != ".iconset":
        ap.error("iconutil wants the folder's name to end in .iconset")

    try:
        master = build_master(args.source)
        with tempfile.TemporaryDirectory() as tmp:
            folder = args.iconset if args.iconset is not None else Path(tmp) / "AppIcon.iconset"
            files = write_iconset(master, folder)
            print(f"Wrote {len(files)} pictures to {folder}")
            if not args.no_icns:
                make_icns(folder, args.out)
                print(f"Wrote {args.out}")
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        print(f"make_mac_appicon: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
