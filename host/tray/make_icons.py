#!/usr/bin/env python3
"""Build the tray icon assets from clawd-tank's own sprite header.

The sprite is clawd-tank's (MIT, Copyright (c) 2026 Marcio Granzotto
Rodrigues; see host/tray/LICENSE-clawd-tank.txt). Upstream stores it as an RLE
RGB565 C array in firmware/main/assets/sprite_idle.h, for the firmware. This
script reads that header (read-only) and writes ordinary PNG files and a
multi-size .ico into host/clawd_tank_tray/assets/, so the running app never
parses a C header.

    py -3 host/tray/make_icons.py                      # rebuild the assets
    py -3 host/tray/make_icons.py --contact-sheet DIR  # also write contact sheets

Format of the header (firmware/main/rle_sprite.h, tools/png2rgb565.py): per
frame a run of (value, count) uint16 pairs, frame_offsets[] gives each frame's
start (plus a sentinel), 0x18C5 (#1a1a2e) is the transparent key.

The tray icon is one still picture per connection state, so this builds only
what the app uses, from the idle sprite's first frame:

    assets/icons/idle-<size>.png    Clawd: the bar is connected
    assets/icons/grey-<size>.png    greyed (desaturated, 60 % alpha): not connected
    assets/icons/error-<size>.png   Clawd with a red dot: not running / port taken
    assets/clawd.ico                the idle Clawd at 16, 20, 24, 32, 40, 48, 64, 256 px
    assets/icons.json               where it came from

The frame is cropped to Clawd's bounding box, padded to a square, and scaled to
each size from that master (never from a smaller size).
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
HEADERS = REPO / "firmware" / "main" / "assets"
ASSETS = REPO / "host" / "clawd_tank_tray" / "assets"

TRANSPARENT_KEY = 0x18C5  # RGB565 of #1a1a2e

# Sizes of the tray pictures. Windows asks for 16 px at 100 % scaling, 20 at
# 125 %, 24 at 150 %, 32 at 200 %; the larger ones serve a taller taskbar and
# very high DPI. The .ico (the exe's icon and the window's) also has 256.
SIZES = (16, 20, 24, 32, 40, 48, 64)
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)

# The one sprite used, and the frame of it that is shown (chosen by eye).
SOURCE = "idle"
SOURCE_FRAME = 0
ICON_NAMES = ("idle", "grey", "error")

# How a shrink is done. The art is flat-colour pixel art at 60-230 px, so a shrink
# must not blend colours (BOX turns the outline and the eyes into brown mush) and
# must not drop features at random (NEAREST misses a 2 px eye between two samples).
#   "mode"    each target pixel takes the most common colour of the block of source
#             pixels it covers: flat colours, stable from frame to frame. Black
#             (the eyes) counts DARK_BOOST times and a colour that is neither body
#             nor shadow (the "!", sparkles, a hat, a bubble) ACCENT_BOOST times, so
#             the small features that make a state readable survive; a block that
#             is mostly transparent stays transparent (OPAQUE_MIN).
#   "box"     average (premultiplied alpha), "nearest" plain sampling.
# Enlarging is always nearest-neighbour.
SHRINK = "mode"
DARK_BOOST = 4        # weight of near-black pixels (the eyes) in the "mode" shrink
DARK_MAX = 4          # "near-black": every channel at most this
ACCENT_BOOST = 2      # weight of a bright colour that is not the body's
SHADOW_MAX = 60       # darker than this (every channel) is shadow or outline: weight 1
OPAQUE_MIN = 0.30     # weighted share of opaque source pixels that makes a target pixel opaque

RED = (226, 49, 49, 255)
RED_RIM = (255, 255, 255, 255)

_NUM = re.compile(r"0x[0-9A-Fa-f]+|\d+")


# --------------------------------------------------------------------------
# Decoding the C headers
# --------------------------------------------------------------------------

def _c_array(text: str, name: str) -> List[int]:
    m = re.search(r"\b" + re.escape(name) + r"\s*\[[^\]]*\]\s*=\s*\{(.*?)\};", text, re.S)
    if not m:
        raise ValueError(f"array {name} not found")
    return [int(t, 0) for t in _NUM.findall(m.group(1))]


def rgb565_to_rgb888(v: int) -> Tuple[int, int, int]:
    """Expand exactly as rle_decode_argb8888() does."""
    r5, g6, b5 = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
    return ((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2))


class Sprite:
    def __init__(self, name: str, width: int, height: int,
                 offsets: Sequence[int], data: Sequence[int]):
        self.name, self.width, self.height = name, width, height
        self.offsets, self.data = offsets, data
        self.frame_count = len(offsets) - 1

    def frame(self, idx: int) -> Image.Image:
        """Decode frame idx to RGBA; the key colour becomes alpha 0."""
        n = self.width * self.height
        px = bytearray(n * 4)
        pos, i = 0, self.offsets[idx]
        while pos < n:
            value, count = self.data[i], self.data[i + 1]
            i += 2
            r, g, b = rgb565_to_rgb888(value)
            a = 0 if value == TRANSPARENT_KEY else 255
            run = min(count, n - pos)
            px[pos * 4:(pos + run) * 4] = bytes((r, g, b, a)) * run
            pos += run
        return Image.frombytes("RGBA", (self.width, self.height), bytes(px))


def load_sprite(name: str, headers: Path = HEADERS) -> Sprite:
    text = (headers / f"sprite_{name}.h").read_text(encoding="utf-8", errors="replace")
    up = name.upper()
    w = int(re.search(rf"#define\s+{up}_WIDTH\s+(\d+)", text).group(1))
    h = int(re.search(rf"#define\s+{up}_HEIGHT\s+(\d+)", text).group(1))
    frames = int(re.search(rf"#define\s+{up}_FRAME_COUNT\s+(\d+)", text).group(1))
    offsets = _c_array(text, f"{name}_frame_offsets")
    data = _c_array(text, f"{name}_rle_data")
    if len(offsets) != frames + 1:
        raise ValueError(f"{name}: {len(offsets)} offsets for {frames} frames")
    if offsets[-1] != len(data):
        raise ValueError(f"{name}: sentinel {offsets[-1]} != data length {len(data)}")
    return Sprite(name, w, h, offsets, data)


# --------------------------------------------------------------------------
# Image work
# --------------------------------------------------------------------------

def bbox(frame: Image.Image) -> Tuple[int, int, int, int]:
    """The box around the visible pixels."""
    box = frame.getchannel("A").getbbox()
    if not box:
        raise ValueError("a sprite with no visible pixel")
    return box


def square_crop(frame: Image.Image, box: Tuple[int, int, int, int]) -> Image.Image:
    """Crop to `box`, then pad to a square (content centred, transparent)."""
    crop = frame.crop(box)
    side = max(crop.size)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    return out


def _body_colour(img: Image.Image) -> Tuple[int, int, int]:
    """The most common opaque colour: Clawd's body."""
    raw = img.tobytes()
    votes: Dict[Tuple[int, int, int], int] = {}
    for r, g, b, a in zip(raw[0::4], raw[1::4], raw[2::4], raw[3::4]):
        if a >= 128:
            votes[(r, g, b)] = votes.get((r, g, b), 0) + 1
    return max(votes, key=votes.get) if votes else (0, 0, 0)


def _mode_shrink(img: Image.Image, size: int) -> Image.Image:
    """Shrink by block: the most common colour of the source block under each
    target pixel (blocks may overlap by a pixel where the ratio is not whole)."""
    side = img.width
    body = _body_colour(img)
    src = img.load()
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dst = out.load()
    for ty in range(size):
        y0 = (ty * side) // size
        y1 = max(y0 + 1, -((-(ty + 1) * side) // size))
        for tx in range(size):
            x0 = (tx * side) // size
            x1 = max(x0 + 1, -((-(tx + 1) * side) // size))
            votes: Dict[Tuple[int, int, int], int] = {}
            clear = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    r, g, b, a = src[x, y]
                    if a < 128:
                        clear += 1
                        continue
                    peak = max(r, g, b)
                    if peak <= DARK_MAX:
                        weight = DARK_BOOST
                    elif peak > SHADOW_MAX and (r, g, b) != body:
                        weight = ACCENT_BOOST
                    else:
                        weight = 1
                    votes[(r, g, b)] = votes.get((r, g, b), 0) + weight
            weight_in = sum(votes.values())
            if weight_in and weight_in / (weight_in + clear) >= OPAQUE_MIN:
                colour = max(votes.items(), key=lambda kv: (kv[1], kv[0]))[0]
                dst[tx, ty] = colour + (255,)
    return out


def scale_to(img: Image.Image, size: int) -> Image.Image:
    """Scale a square RGBA image to size x size (see SHRINK)."""
    if img.width == size:
        return img.copy()
    if size >= img.width or SHRINK == "nearest":
        return img.resize((size, size), Image.NEAREST)
    if SHRINK == "box":
        # Pillow averages RGBA on premultiplied alpha, so transparency never
        # bleeds its colour into an edge pixel.
        return img.resize((size, size), Image.BOX)
    return _mode_shrink(img, size)


def greyed(img: Image.Image) -> Image.Image:
    """Desaturated, 60 % alpha: Clawd with no bar to talk to."""
    rgba = img.convert("RGBA")
    lum = rgba.convert("L")
    # Lift the darks a little so the dark outline does not read as a hole.
    lum = lum.point(lambda v: 60 + (v * 150) // 255)
    out = Image.merge("RGBA", (lum, lum, lum, rgba.getchannel("A").point(
        lambda v: (v * 153 + 127) // 255)))
    return out


def with_red_dot(img: Image.Image) -> Image.Image:
    """Clawd plus a red dot (white rim) in the bottom-right corner."""
    size = img.width
    out = img.copy()
    d = ImageDraw.Draw(out)
    r = max(3, round(size * (0.22 if size <= 24 else 0.18)))
    cx = cy = size - r - 1
    if size >= 24:
        d.ellipse([cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1], fill=RED_RIM)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=RED)
    return out


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build_master(headers: Path) -> Tuple[Image.Image, dict]:
    """The square master (native pixels) of the one frame that is used, plus what
    to record about it."""
    sp = load_sprite(SOURCE, headers)
    raw = sp.frame(SOURCE_FRAME)
    box = bbox(raw)
    master = square_crop(raw, box)
    info = {"source": f"sprite_{SOURCE}.h", "source_frames": sp.frame_count,
            "frame": SOURCE_FRAME, "bbox": list(box), "master": master.width}
    return master, info


def icons_at(master: Image.Image, size: int) -> Dict[str, Image.Image]:
    """name -> picture at `size`: Clawd, greyed, and with a red dot."""
    idle = scale_to(master, size)
    return {"idle": idle, "grey": greyed(idle), "error": with_red_dot(idle)}


def write_assets(headers: Path = HEADERS, assets: Path = ASSETS) -> dict:
    master, info = build_master(headers)
    icons_dir = assets / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    for old in icons_dir.glob("*.png"):
        old.unlink()
    for size in SIZES:
        for name, image in icons_at(master, size).items():
            image.save(icons_dir / f"{name}-{size}.png", optimize=True)

    # The .ico: the idle Clawd at every size, each one scaled from the master
    # (not shrunk from the 256), so small sizes stay crisp.
    ico_frames = [icons_at(master, s)["idle"] for s in ICO_SIZES]
    ico_frames[-1].save(assets / "clawd.ico", format="ICO",
                        sizes=[(s, s) for s in ICO_SIZES],
                        append_images=ico_frames[:-1])
    manifest = {"sizes": list(SIZES), "ico_sizes": list(ICO_SIZES),
                "transparent_key": f"0x{TRANSPARENT_KEY:04X}", "shrink": SHRINK,
                "icons": list(ICON_NAMES), "source": info}
    (assets / "icons.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                       encoding="utf-8", newline="\n")
    return manifest


# --------------------------------------------------------------------------
# Contact sheets
# --------------------------------------------------------------------------

CONTACT_SIZES = (16, 24, 32, 64)
STATES = (("idle", "connected"), ("grey", "not connected"), ("error", "error"))


def contact_sheet(assets: Path, out_dir: Path, zoom: int = 1) -> List[Path]:
    """One PNG per background: the three tray states at 16, 24, 32 and 64 px,
    left to right, on a dark and on a light background, as actual pixels (zoom 1)
    or enlarged with nearest-neighbour (zoom > 1)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for bg_name, bg in (("dark", (32, 32, 32, 255)), ("light", (243, 243, 243, 255))):
        pad = 8 * zoom
        label_w = 112
        col_w = [s * zoom + pad for s in CONTACT_SIZES]
        row_h = max(CONTACT_SIZES) * zoom + pad
        w = label_w + sum(col_w) + pad
        h = row_h * len(STATES) + pad
        sheet = Image.new("RGBA", (w, h), bg)
        d = ImageDraw.Draw(sheet)
        fg = (230, 230, 230, 255) if bg_name == "dark" else (30, 30, 30, 255)
        for r, (name, label) in enumerate(STATES):
            y0 = pad + r * row_h
            d.text((6, y0 + row_h // 2 - 8), label, fill=fg)
            x = label_w
            for size, cw in zip(CONTACT_SIZES, col_w):
                frame = Image.open(assets / "icons" / f"{name}-{size}.png").convert("RGBA")
                if zoom > 1:
                    frame = frame.resize((size * zoom, size * zoom), Image.NEAREST)
                sheet.alpha_composite(frame, (x, y0 + (row_h - pad - frame.height) // 2))
                x += cw
        path = out_dir / f"contact-{bg_name}-{'x%d' % zoom}.png"
        sheet.save(path)
        written.append(path)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--headers", type=Path, default=HEADERS,
                    help="folder with sprite_*.h (default: firmware/main/assets)")
    ap.add_argument("--assets", type=Path, default=ASSETS,
                    help="output folder (default: host/clawd_tank_tray/assets)")
    ap.add_argument("--contact-sheet", type=Path, default=None, metavar="DIR",
                    help="also write contact sheets of the three tray states (16/24/32/64 px, "
                         "at 1x and 6x) into DIR")
    args = ap.parse_args(argv)
    manifest = write_assets(args.headers, args.assets)
    n = sum(1 for _ in (args.assets / "icons").glob("*.png"))
    print(f"wrote {n} icons and clawd.ico into {args.assets}")
    src = manifest["source"]
    print(f"  from {src['source']} frame {src['frame']} of {src['source_frames']}, "
          f"master {src['master']}px")
    if args.contact_sheet:
        for z in (1, 6):
            for p in contact_sheet(args.assets, args.contact_sheet, z):
                print("contact sheet:", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
