#!/usr/bin/env python3
"""
Recolour only Clawd's orange body family; props and the black eyes stay as-is.

Where the colour comes from (see colour-analysis.txt):
  * SVG fill            #DE886D  (222,136,109)
  * svg2frames.py's snap_pixel_art() floors every solid pixel to a 16-step
    palette -> (208,128,96) = #D08060  (the "about #D08060" the bar shows)
  * png2rgb565.py truncates to RGB565 (0xD40C); the panel expands that back
    to (214,130,99) = #D68263
The body is ONE flat colour per frame (no highlight / shadow shades in the
art). The only other body colours are:
  * soft edge pixels with alpha 7..78 % (CSS-transform anti-aliasing): RGB stays
    the unsnapped body colour, alpha carries the "shade"; png2rgb565 later
    blends them over the sky (#1A1A2E). We keep alpha untouched, so the
    shading ratio is preserved exactly.
  * whole-body tint frames (alert frame 31 = +R highlight, idle f83 / sleeping
    f14 = +B warm tint): solid pixels at a snapped palette neighbour of the
    base; mapped by the per-channel ratio to the base, so they stay a
    highlight / warm tint of the new base.

Two entry points:
  recolour_rgba(arr, target)   PNG stage (before png2rgb565.py) - the production path
  recolour_decoded(arr, target) decoded RGB565->888 frames (mini-crab HUD icon etc.)
"""
import numpy as np

RAW = np.array([222, 136, 109], dtype=np.float64)    # SVG fill #DE886D
SNAP = np.array([208, 128, 96], dtype=np.float64)    # after the 16-step snap
PANEL_BASE = (214, 130, 99)                          # RGB565 0xD40C expanded
BG = np.array([0x1A, 0x1A, 0x2E], dtype=np.float64)  # sky colour png2rgb565 blends over

# user's option targets (hex = intent; the panel only shows RGB565, so each is
# snapped to the nearest colour RGB565 can actually display)
OPTIONS = {
    "A": ("current", None),
    "B": ("bright orange", "#FF8A1F"),
    "C": ("deeper warm orange", "#F26B0F"),
    "D": ("Claude-brand-leaning bright", "#FF7A3D"),
}


def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb2hex(c):
    return "#%02X%02X%02X" % tuple(int(round(v)) for v in c)


def _expand5(k):
    return (k << 3) | (k >> 2)


def _expand6(k):
    return (k << 2) | (k >> 4)


def nearest565(rgb):
    """Nearest colour the RGB565 panel can display, as the expanded 8-bit RGB."""
    r, g, b = rgb
    kr = min(range(32), key=lambda k: abs(_expand5(k) - r))
    kg = min(range(64), key=lambda k: abs(_expand6(k) - g))
    kb = min(range(32), key=lambda k: abs(_expand5(k) - b))
    return (_expand5(kr), _expand6(kg), _expand5(kb))


def rgb565_word(rgb):
    r, g, b = rgb
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def target_for(opt):
    """On-panel target colour (nearest displayable) for an option letter, or None for A."""
    h = OPTIONS[opt][1]
    return None if h is None else nearest565(hex2rgb(h))


# props that are warm-brown and must never be touched: the debugger magnifier's handle
# (#795548 -> 16-step floor (112,80,64); raw edge pixels near (121,85,72))
HANDLE_SNAP = np.array([112, 80, 64], dtype=np.float64)
HANDLE_RAW = np.array([121, 85, 72], dtype=np.float64)


def _masks_rgba(arr, tol=26.0, shade_tol=18.0):
    """(near, shade) masks of body-family pixels in an RGBA PNG frame.

    near  : within `tol` RGB units of the snapped base (208,128,96) or the SVG fill (222,136,109):
            the flat body, the whole-body tint frames, and the translucent soft edges.
    shade : the 1 px dark fringe where the body's anti-aliased edge met black (eyes, legs, shadow):
            c = k * base with 0.3 <= k < 1 (k against the snapped base for solid pixels, against the
            fill for translucent ones), warm hue, not a known warm-brown prop.
    """
    rgb = arr[:, :, :3].astype(np.float64)
    a = arr[:, :, 3]
    d_raw = np.linalg.norm(rgb - RAW, axis=2)
    d_snap = np.linalg.norm(rgb - SNAP, axis=2)
    near = (a > 0) & (np.minimum(d_raw, d_snap) <= tol)
    ref = np.where((a == 255)[:, :, None], SNAP, RAW)
    k = (rgb * ref).sum(axis=2) / (ref * ref).sum(axis=2)
    resid = np.linalg.norm(rgb - k[:, :, None] * ref, axis=2)
    warm = (rgb[:, :, 0] - rgb[:, :, 2] > 25) & (rgb[:, :, 0] > rgb[:, :, 1]) & (rgb[:, :, 1] >= rgb[:, :, 2] - 5)
    handle = (np.linalg.norm(rgb - HANDLE_SNAP, axis=2) <= 10) | (np.linalg.norm(rgb - HANDLE_RAW, axis=2) <= 14)
    shade = (a > 0) & warm & (k >= 0.3) & (k < 1.0) & (resid <= shade_tol) & ~near & ~handle
    return near, shade


def body_mask_rgba(arr, tol=26.0):
    """Mask of body-family pixels in an RGBA PNG frame (alpha > 0)."""
    near, shade = _masks_rgba(arr, tol)
    return near | shade


def recolour_rgba(arr, target):
    """Return a recoloured copy of an RGBA uint8 frame. alpha is never touched."""
    out = arr.copy()
    rgb = arr[:, :, :3].astype(np.float64)
    a = arr[:, :, 3]
    near, shade = _masks_rgba(arr)
    t = np.array(target, dtype=np.float64)
    solid = (a == 255)[:, :, None]
    # solid (snapped) pixels: per-channel ratio to the snapped base (tint frames and the dark fringe
    # keep their highlight / warm tint / darkness); translucent soft edges: the plain target;
    # translucent dark-fringe pixels: the target scaled by their ratio to the SVG fill.
    ratio = np.where(solid, rgb / SNAP, np.where(shade[:, :, None], rgb / RAW, 1.0))
    new = np.clip(t * ratio, 0, 255)
    # a flat body pixel is exactly the target
    flat = (np.linalg.norm(rgb - SNAP, axis=2) < 0.5) & (a == 255)
    new[flat] = t
    m = near | shade
    out[:, :, :3] = np.where(m[:, :, None], np.rint(new).astype(np.uint8), arr[:, :, :3])
    return out


def recolour_decoded(arr, target, tol=7.0):
    """Recolour a decoded (RGB565 -> 888) RGBA frame: exact base, tint, and bg->body blends."""
    out = arr.copy()
    rgb = arr[:, :, :3].astype(np.float64)
    t = np.array(target, dtype=np.float64)
    base = np.array(PANEL_BASE, dtype=np.float64)
    solid = np.linalg.norm(rgb - base, axis=2) <= 20
    # bg -> body alpha blends: c = bg + a*(RAW-bg)
    d = RAW - BG
    a = ((rgb - BG) @ d) / (d @ d)
    resid = np.linalg.norm(rgb - (BG + a[:, :, None] * d), axis=2)
    blend = (a >= 0.05) & (a <= 0.95) & (resid <= tol) & (rgb[:, :, 0] > rgb[:, :, 1] + 8) & ~solid
    new = rgb.copy()
    new[solid] = np.clip(t * (rgb[solid] / base), 0, 255)
    new[blend] = BG + a[blend][:, None] * (t - BG)
    # body mixed with black (eye / shadow edge anti-aliasing): c = k * base, k < 1.
    # Used for the shipped mini-crab icon, which has such pixels; the orange-hue
    # test keeps the blue particle, the shadow and the black eyes out.
    k = (rgb @ base) / (base @ base)
    resid_k = np.linalg.norm(rgb - k[:, :, None] * base, axis=2)
    orange = (rgb[:, :, 0] > rgb[:, :, 1] + 18) & (rgb[:, :, 1] >= rgb[:, :, 2] - 6)
    shade = orange & (k >= 0.3) & (k < 1.0) & (resid_k <= 14) & ~solid & ~blend
    new[shade] = np.clip(k[shade][:, None] * t, 0, 255)
    m = (solid | blend | shade) & (arr[:, :, 3] > 0)
    out[:, :, :3] = np.where(m[:, :, None], np.rint(new).astype(np.uint8), arr[:, :, :3])
    return out


def shade_table(target):
    """Rows (label, source on-panel hex, new on-panel hex) for the report."""
    t = np.array(target, dtype=np.float64)
    rows = []

    def q(c):
        c = tuple(int(round(v)) for v in c)
        return nearest565(c)

    base = np.array(PANEL_BASE, dtype=np.float64)
    rows.append(("body base (flat, 80-99 % of body px)", PANEL_BASE, q(t)))
    for label, src in (("highlight frame (alert f31, +R)", (231, 130, 99)),
                       ("warm-tint frames (idle f83, sleeping f14, +B)", (214, 130, 115))):
        rows.append((label, src, q(np.clip(t * (np.array(src) / base), 0, 255))))
    for al in (0.75, 0.50, 0.25):
        src = BG + al * (RAW - BG)
        rows.append((f"soft edge, {int(al * 100)} % alpha over sky", tuple(int(round(v)) for v in src),
                     q(BG + al * (t - BG))))
    for k in (0.77, 0.62, 0.50):
        rows.append((f"dark fringe pixel, {int(k * 100)} % of base (eye / leg edge)", q(SNAP * k), q(t * k)))
    return rows


def target_from_hex(hexstr):
    """On-panel (nearest RGB565) colour for a #RRGGBB string: what the body becomes."""
    return nearest565(hex2rgb(hexstr))
