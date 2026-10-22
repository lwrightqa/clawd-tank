#!/usr/bin/env python3
"""
build_bar_sprites.py: rebuild the sprite headers for the MiniBar bar (bigger, brighter Clawd).

The upstream pipeline is svg2frames.py -> png2rgb565.py -> crop_sprites.py at 4 px per SVG unit. The bar draws Clawd at
1.5x (6 px per unit) in a brighter orange, so this runs the same three tools at another scale, with one extra step
between the first and the second: clawd_recolour.py repaints Clawd's orange body (props and eyes stay as they are).

    py -3 tools/build_bar_sprites.py --out build/sprites                     # 1.5x, #F17D4A, every animated sprite
    py -3 tools/build_bar_sprites.py --out build/sprites --only typing,idle  # a few
    py -3 tools/build_bar_sprites.py --install                               # write into firmware/main/assets

Defaults are what the firmware ships: scale 6 for every sprite and the body colour in --colour (RGB565 cannot show
every hex, so the nearest displayable value is used and printed). The "No connection" screen is drawn from
assets/svg-animations/bar/clawd-disconnected-bar.svg: upstream's Bluetooth icon is too tall for Clawd at 1.5x. The
HUD's small crab (sprite_mini_crab.h) is decoded from the header already in assets/, recoloured and written back. The y_offset values in scene.c follow the rule in the comment above anim_defs; this script reports the cropped
rows that rule needs.

Needs Playwright with Chromium (svg2frames.py), Pillow and numpy.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import clawd_recolour as rc  # noqa: E402

SVG_DIR = REPO / "assets" / "svg-animations"
ASSETS = REPO / "firmware" / "main" / "assets"

# name -> (svg stem, fps, duration in seconds); frames = round(fps * duration), as in the shipped headers
SPRITES = {
    "typing":       ("clawd-working-typing",     8, 1.5),
    "debugger":     ("clawd-working-debugger",   8, 12.0),
    "thinking":     ("clawd-working-thinking",   8, 4.0),
    "conducting":   ("clawd-working-conducting", 8, 2.0),
    "alert":        ("clawd-notification",      10, 4.0),
    "sleeping":     ("clawd-sleeping",           6, 6.0),
    "idle":         ("clawd-idle-living",        6, 16.0),
    "disconnected": ("clawd-disconnected",       6, 6.0),
    "happy":        ("clawd-happy",             10, 2.0),
    "going_away":   ("clawd-going-away",         8, 15 / 8),
    "juggling":     ("clawd-working-juggling",   8, 1.25),
    "building":     ("clawd-working-building",   8, 1.0),
    "confused":     ("clawd-working-confused",   8, 6.0),
    "dizzy":        ("clawd-dizzy",              8, 4.0),
    "sweeping":     ("clawd-working-sweeping",   8, 1.5),
    "walking":      ("clawd-crab-walking",       8, 1.0),
    "wizard":       ("clawd-working-wizard",     8, 6.0),
    "beacon":       ("clawd-working-beacon",     8, 8.0),
}
# conducting's particle stream has positive animation delays, so its first loop is half empty: sample one cycle later
SEEK_OFFSET_MS = {"conducting": 2000}
# sprites drawn from a bar-specific copy of the SVG (assets/svg-animations/bar/): the No connection screen's Bluetooth
# icon is smaller and lower so that Clawd can be 1.5x like the others on the 172 px tall screen
BAR_SVG = {"disconnected": SVG_DIR / "bar" / "clawd-disconnected-bar.svg"}


def _render(job):
    name, scale, work = job
    import svg2frames as s
    off = SEEK_OFFSET_MS.get(name, 0)
    if off:
        orig = s.build_html_wrapper

        def patched(svg_path, sc, bg):
            html, w, h = orig(svg_path, sc, bg)
            return html.replace("window.seekAnimations = function(timeMs) {",
                                "window.seekAnimations = function(timeMs) { timeMs += %d;" % off), w, h
        s.build_html_wrapper = patched
    svg, fps, dur = SPRITES[name]
    out = work / "frames" / name
    for attempt in range(3):
        saved, _, _ = s.render_frames(BAR_SVG.get(name, SVG_DIR / f"{svg}.svg"), out, fps, dur, float(scale), "transparent")
        # a browser that has not painted yet gives blank frames (seen with eight rendering at once): try again
        if any(Image.open(p).convert("RGBA").getchannel("A").getbbox() for p in saved):
            return name, len(saved)
    raise RuntimeError(f"{name}: three renders in a row gave blank frames")


def _convert(job):
    name, work, target, out_dir = job
    src = work / "frames" / name
    if target is not None:
        dst = work / "frames_recoloured" / name
        dst.mkdir(parents=True, exist_ok=True)
        for p in sorted(src.glob("frame_*.png")):
            arr = np.array(Image.open(p).convert("RGBA"))
            Image.fromarray(rc.recolour_rgba(arr, target), "RGBA").save(dst / p.name)
        src = dst
    hdr = out_dir / f"sprite_{name}.h"
    r = subprocess.run([sys.executable, str(HERE / "png2rgb565.py"), str(src), str(hdr), "--name", name],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"png2rgb565 failed for {name}: {r.stdout[-300:]} {r.stderr[-300:]}")
    import crop_sprites
    res = crop_sprites.process_sprite(hdr, dry_run=False)
    if res.get("skipped"):
        raise RuntimeError(f"{name}: nothing to crop ({res.get('reason')}); refusing to leave a blank sprite behind")
    # the tools write native line endings; the repo is LF
    hdr.write_bytes(hdr.read_bytes().replace(b"\r\n", b"\n"))
    return name, res


def _decode_header(path):
    """(prefix, width, height, frames as RGBA arrays) from an RLE RGB565 header (key colour -> alpha 0)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    prefix = re.search(r"#define\s+(\w+)_WIDTH", text).group(1)
    w = int(re.search(rf"#define\s+{prefix}_WIDTH\s+(\d+)", text).group(1))
    h = int(re.search(rf"#define\s+{prefix}_HEIGHT\s+(\d+)", text).group(1))
    num = re.compile(r"0x[0-9A-Fa-f]+|\d+")

    def arr(name):
        m = re.search(r"\b" + name + r"\s*\[[^\]]*\]\s*=\s*\{(.*?)\};", text, re.S)
        return [int(t, 0) for t in num.findall(m.group(1))]
    offsets, data = arr(prefix.lower() + "_frame_offsets"), arr(prefix.lower() + "_rle_data")
    frames = []
    for f in range(len(offsets) - 1):
        px, i = [], offsets[f]
        while len(px) < w * h:
            v, c = data[i], data[i + 1]
            i += 2
            r5, g6, b5 = (v >> 11) & 31, (v >> 5) & 63, v & 31
            col = ((r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2), 0 if v == 0x18C5 else 255)
            px.extend([col] * c)
        frames.append(np.array(px[:w * h], dtype=np.uint8).reshape(h, w, 4))
    return prefix.lower(), w, h, frames


def recolour_mini_crab(src, dst, target, work):
    """The HUD's small crab: decode the header at src, repaint it, write a new header at dst."""
    name, w, h, frames = _decode_header(src)
    d = work / "frames" / name
    d.mkdir(parents=True, exist_ok=True)
    for i, a in enumerate(frames):
        Image.fromarray(rc.recolour_decoded(a, target), "RGBA").save(d / f"frame_{i:02d}.png")
    tmp = work / f"sprite_{name}.h"
    r = subprocess.run([sys.executable, str(HERE / "png2rgb565.py"), str(d), str(tmp), "--name", name],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("png2rgb565 failed for the mini crab: " + r.stdout[-300:] + r.stderr[-300:])
    dst.write_bytes(tmp.read_bytes().replace(b"\r\n", b"\n"))
    return w, h


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, help="where the headers go (default: <work>/headers)")
    ap.add_argument("--work", type=Path, default=REPO / "build" / "sprite-work", help="frames and scratch files")
    ap.add_argument("--install", action="store_true", help="write into firmware/main/assets")
    ap.add_argument("--scale", type=int, default=6, help="px per SVG unit (4 = upstream's size, 6 = 1.5x)")
    ap.add_argument("--disconnected-scale", type=int, default=6, help="px per unit for the No connection screen")
    ap.add_argument("--colour", default="#F17D4A", help="body colour; 'none' keeps upstream's salmon")
    ap.add_argument("--only", default="", help="comma-separated sprite names (default: all)")
    ap.add_argument("--skip-mini-crab", action="store_true")
    args = ap.parse_args()
    out_dir = ASSETS if args.install else (args.out or args.work / "headers")
    out_dir.mkdir(parents=True, exist_ok=True)
    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    target = None if args.colour.lower() == "none" else rc.target_from_hex(args.colour)
    names = [n for n in SPRITES if not args.only or n in args.only.split(",")]
    t0 = time.time()
    body = "upstream salmon" if target is None else "#%02X%02X%02X on the panel" % tuple(target)
    print(f"scale {args.scale} ({args.disconnected_scale} for disconnected), body {body}, "
          f"{len(names)} sprites -> {out_dir}")

    jobs = [(n, args.disconnected_scale if n == "disconnected" else args.scale, work) for n in names]
    with ProcessPoolExecutor(max_workers=8) as ex:
        for n, count in ex.map(_render, jobs):
            print(f"  rendered {n:13s} {count:3d} frames")
    results = {}
    with ProcessPoolExecutor(max_workers=8) as ex:
        for n, res in ex.map(_convert, [(n, work, target, out_dir) for n in names]):
            results[n] = res
    print("\nsprite          WxH   bottom rows cropped (for the y_offset rule in scene.c)")
    for n in names:
        r = results[n]
        print(f"  {n:13s} {r['new_w']:>3d}x{r['new_h']:<3d} {r['bottom_removed']:>4d}")
    if not args.skip_mini_crab and target is not None:
        src = HERE / "upstream_sprites" / "sprite_mini_crab.h"   # upstream's own, so a rebuild never recolours a recolour
        w, h = recolour_mini_crab(src, out_dir / "sprite_mini_crab.h", target, work)
        print(f"  mini_crab     {w}x{h} recoloured from tools/upstream_sprites/{src.name}")
    json.dump(results, open(work / "crop.json", "w"), indent=1)
    print(f"done in {time.time() - t0:.0f} s; crop data in {work / 'crop.json'}")


if __name__ == "__main__":
    main()
