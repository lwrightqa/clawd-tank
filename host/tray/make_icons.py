#!/usr/bin/env python3
"""Build the tray icon assets from clawd-tank's own sprite headers.

The sprites are clawd-tank's (MIT, Copyright (c) 2026 Marcio Granzotto
Rodrigues; see host/tray/LICENSE-clawd-tank.txt). Upstream stores them as RLE
RGB565 C arrays in firmware/main/assets/sprite_*.h, for the firmware. This
script reads those headers (read-only) and writes ordinary PNG files and a
multi-size .ico into host/clawd_tank_tray/assets/, so the running app never
parses a C header.

    py -3 host/tray/make_icons.py                      # rebuild the assets
    py -3 host/tray/make_icons.py --contact-sheet DIR  # also write contact sheets

Format of the headers (firmware/main/rle_sprite.h, tools/png2rgb565.py): per
frame a run of (value, count) uint16 pairs, frame_offsets[] gives each frame's
start (plus a sentinel), 0x18C5 (#1a1a2e) is the transparent key.

What is written, for every sprite the tray shows:

    assets/frames/<sprite>-<size>.png   a horizontal strip of N square frames
    assets/clawd.ico                    16, 20, 24, 32, 40, 48, 64 and 256 px
    assets/frames.json                  frame counts and source frame indices

Per sprite the frames that are kept are spaced evenly through the animation.
All of them are cropped to ONE bounding box (the union over the kept frames, so
Clawd does not change size from frame to frame), padded to a square, and
scaled to each size. Two derived sprites are made from the idle one: ``grey``
(desaturated, 60 % alpha: no bar) and ``error`` (Clawd with a red dot).
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

# Sizes the tray and the window use. Windows asks for 16 px at 100 % scaling,
# 20 at 125 %, 24 at 150 %, 32 at 200 %; the others serve the window, the
# taskbar and the Start menu.
SIZES = (16, 20, 24, 32, 40, 48, 64)
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)

# sprite -> how many frames to keep (evenly spaced), or an explicit index list.
# idle and sleeping are shown still: one frame, chosen by eye.
KEEP: Dict[str, object] = {
    "idle": [0],
    "sleeping": [21],
    "thinking": 8,
    "typing": 8,
    "debugger": 8,
    "building": 8,
    "conducting": 8,
    "wizard": 8,
    "beacon": 8,
    "confused": 8,
    "sweeping": 6,
    "happy": 6,
    "alert": 8,
    "dizzy": 8,
}

# How a size-by-size shrink is done. The art is chunky pixels at 40-190 px; at
# 16-32 px "nearest" drops whole features at random, so shrinking averages the
# covered pixels (BOX) and only enlarging is nearest-neighbour.
SHRINK = "box"

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

def pick_frames(count: int, keep) -> List[int]:
    """Indices to keep: an explicit list, or `keep` frames spaced evenly (the
    last frame is skipped so that the loop closes without a double frame)."""
    if isinstance(keep, (list, tuple)):
        return [int(i) for i in keep]
    keep = max(1, min(int(keep), count))
    return sorted({(i * count) // keep for i in range(keep)})


def union_bbox(frames: Sequence[Image.Image]) -> Tuple[int, int, int, int]:
    boxes = [f.getchannel("A").getbbox() for f in frames]
    boxes = [b for b in boxes if b]
    if not boxes:
        raise ValueError("a sprite with no visible pixel")
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def square_crop(frame: Image.Image, box: Tuple[int, int, int, int]) -> Image.Image:
    """Crop to `box`, then pad to a square (content centred, transparent)."""
    crop = frame.crop(box)
    side = max(crop.size)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))
    return out


def scale_to(img: Image.Image, size: int) -> Image.Image:
    """Scale a square RGBA image to size x size. Enlarging is nearest-neighbour
    (crisp pixels). Shrinking averages with premultiplied alpha, so a
    transparent neighbour never bleeds its colour into the edge."""
    if img.width == size:
        return img.copy()
    if size >= img.width or SHRINK == "nearest":
        return img.resize((size, size), Image.NEAREST)
    r, g, b, a = img.split()
    pre = Image.merge("RGBA", (
        Image.composite(r, Image.new("L", img.size, 0), a),
        Image.composite(g, Image.new("L", img.size, 0), a),
        Image.composite(b, Image.new("L", img.size, 0), a),
        a,
    ))
    small = pre.resize((size, size), Image.BOX)
    sr, sg, sb, sa = small.split()
    px_a = sa.load()
    out = Image.new("RGBA", (size, size))
    px_o = out.load()
    p = [c.load() for c in (sr, sg, sb)]
    for y in range(size):
        for x in range(size):
            alpha = px_a[x, y]
            if alpha == 0:
                px_o[x, y] = (0, 0, 0, 0)
                continue
            px_o[x, y] = tuple(min(255, (p[k][x, y] * 255 + alpha // 2) // alpha)
                               for k in range(3)) + (alpha,)
    return out


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


def strip(frames: Sequence[Image.Image]) -> Image.Image:
    size = frames[0].width
    out = Image.new("RGBA", (size * len(frames), size), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        out.paste(f, (i * size, 0))
    return out


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build_masters(headers: Path) -> Tuple[Dict[str, List[Image.Image]], Dict[str, dict]]:
    """name -> list of square master frames (native pixels), plus the manifest."""
    masters: Dict[str, List[Image.Image]] = {}
    info: Dict[str, dict] = {}
    for name, keep in KEEP.items():
        sp = load_sprite(name, headers)
        idxs = pick_frames(sp.frame_count, keep)
        raw = [sp.frame(i) for i in idxs]
        box = union_bbox(raw)
        masters[name] = [square_crop(f, box) for f in raw]
        info[name] = {"source": f"sprite_{name}.h", "source_frames": sp.frame_count,
                      "kept": idxs, "bbox": list(box), "master": masters[name][0].width}
    return masters, info


def sized(masters: Dict[str, List[Image.Image]], size: int) -> Dict[str, List[Image.Image]]:
    out = {name: [scale_to(f, size) for f in frames] for name, frames in masters.items()}
    idle = out["idle"][0]
    out["grey"] = [greyed(idle)]
    out["error"] = [with_red_dot(idle)]
    return out


def write_assets(headers: Path = HEADERS, assets: Path = ASSETS) -> dict:
    masters, info = build_masters(headers)
    frames_dir = assets / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.png"):
        old.unlink()
    per_size = {}
    for size in SIZES:
        per_size[size] = sized(masters, size)
        for name, frames in per_size[size].items():
            strip(frames).save(frames_dir / f"{name}-{size}.png", optimize=True)
    info["grey"] = {"derived_from": "idle", "kept": [0]}
    info["error"] = {"derived_from": "idle", "kept": [0]}

    # The .ico: the idle frame at every size, each one scaled from the master
    # (not shrunk from the 256), so small sizes stay crisp.
    ico_frames = [sized({"idle": masters["idle"]}, s)["idle"][0] for s in ICO_SIZES]
    ico_frames[-1].save(assets / "clawd.ico", format="ICO",
                        sizes=[(s, s) for s in ICO_SIZES],
                        append_images=ico_frames[:-1])
    manifest = {"sizes": list(SIZES), "ico_sizes": list(ICO_SIZES),
                "transparent_key": f"0x{TRANSPARENT_KEY:04X}",
                "shrink": SHRINK, "sprites": info}
    (assets / "frames.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                        encoding="utf-8", newline="\n")
    return manifest


# --------------------------------------------------------------------------
# Contact sheets
# --------------------------------------------------------------------------

CONTACT_SIZES = (16, 24, 32, 64)
STATES = ["idle", "sleeping", "thinking", "typing", "debugger", "building", "conducting",
          "wizard", "beacon", "confused", "sweeping", "happy", "alert", "dizzy",
          "grey", "error"]


def contact_sheet(assets: Path, out_dir: Path, zoom: int = 1) -> List[Path]:
    """One PNG per background: every state (first frame) at 16, 24, 32 and 64
    px, left to right, on a dark and on a light background, as actual pixels
    (zoom 1) or enlarged with nearest-neighbour (zoom > 1)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for bg_name, bg in (("dark", (32, 32, 32, 255)), ("light", (243, 243, 243, 255))):
        pad = 8 * zoom
        label_w = 96
        col_w = [s * zoom + pad for s in CONTACT_SIZES]
        row_h = max(CONTACT_SIZES) * zoom + pad
        w = label_w + sum(col_w) + pad
        h = row_h * len(STATES) + pad
        sheet = Image.new("RGBA", (w, h), bg)
        d = ImageDraw.Draw(sheet)
        fg = (230, 230, 230, 255) if bg_name == "dark" else (30, 30, 30, 255)
        for r, state in enumerate(STATES):
            y0 = pad + r * row_h
            d.text((6, y0 + row_h // 2 - 8), state, fill=fg)
            x = label_w
            for size, cw in zip(CONTACT_SIZES, col_w):
                strip_img = Image.open(assets / "frames" / f"{state}-{size}.png").convert("RGBA")
                frame = strip_img.crop((0, 0, size, size))
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
                    help="also write contact sheets (1x and 6x) of every state into DIR")
    args = ap.parse_args(argv)
    manifest = write_assets(args.headers, args.assets)
    n = sum(1 for _ in (args.assets / "frames").glob("*.png"))
    print(f"wrote {n} strips and clawd.ico into {args.assets}")
    for name, meta in manifest["sprites"].items():
        print(f"  {name:10s} frames {len(meta['kept']):2d}  {meta.get('source', 'derived')}")
    if args.contact_sheet:
        for z in (1, 6):
            for p in contact_sheet(args.assets, args.contact_sheet, z):
                print("contact sheet:", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
