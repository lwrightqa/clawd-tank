"""The Clawd icons: built from clawd-tank's sprite headers, read from PNG at run time."""

import json
import struct
import sys
from pathlib import Path

import pytest
from PIL import Image

from clawd_tank_tray import icons as icons_mod
from clawd_tank_tray.icons import SIZES, SPRITES, IconSet, nearest_size
from clawd_tank_tray.state import ANIM_SPRITES

HOST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST / "tray"))
import make_icons  # noqa: E402  (the build script, host/tray/make_icons.py)

ASSETS = HOST / "clawd_tank_tray" / "assets"
HEADERS = HOST.parent / "firmware" / "main" / "assets"

STILL = {"idle", "sleeping", "grey", "error"}


@pytest.fixture(scope="module")
def icons():
    return IconSet()


def pixels(img: Image.Image):
    """[(r, g, b, a), ...] of an RGBA image."""
    raw = img.convert("RGBA").tobytes()
    return list(zip(raw[0::4], raw[1::4], raw[2::4], raw[3::4]))


def opaque(img: Image.Image) -> int:
    return sum(1 for a in img.getchannel("A").tobytes() if a > 0)


# --- sizes ---------------------------------------------------------------------


@pytest.mark.parametrize("name", SPRITES)
@pytest.mark.parametrize("size", SIZES)
def test_every_sprite_has_square_non_empty_frames_at_every_size(icons, name, size):
    count = icons.frame_count(name)
    assert count >= 1
    for i in range(count):
        frame = icons.frame(name, i, size)
        assert frame.size == (size, size) and frame.mode == "RGBA"
        assert opaque(frame) > 0, f"{name} frame {i} at {size}px is empty"


def test_frame_counts_are_the_same_at_every_size(icons):
    for name in SPRITES:
        assert {len(icons._strip(name, s)) for s in SIZES} == {icons.frame_count(name)}


def test_still_and_animated_sprites(icons):
    for name in SPRITES:
        if name in STILL:
            assert icons.frame_count(name) == 1 and not icons.is_animated(name)
        else:
            assert icons.frame_count(name) >= 6 and icons.is_animated(name), name


def test_animation_frame_counts_match_the_manifest(icons):
    manifest = json.loads((ASSETS / "frames.json").read_text(encoding="utf-8"))
    assert manifest["sizes"] == list(SIZES)
    for name in SPRITES:
        assert len(manifest["sprites"][name]["kept"]) == icons.frame_count(name), name
    # and each kept frame is a distinct index of the source animation
    for name, meta in manifest["sprites"].items():
        kept = meta["kept"]
        assert kept == sorted(set(kept))
        if "source_frames" in meta:
            assert all(0 <= i < meta["source_frames"] for i in kept)


@pytest.mark.parametrize("name", sorted(set(SPRITES) - STILL))
def test_animation_frames_actually_differ(icons, name):
    frames = [icons.frame(name, i, 32).tobytes() for i in range(icons.frame_count(name))]
    assert len(set(frames)) > 1, f"{name} would look frozen"


def test_frames_wrap_around(icons):
    n = icons.frame_count("thinking")
    assert icons.frame("thinking", n, 24) is icons.frame("thinking", 0, 24)
    assert icons.frame("thinking", 0, 24) is icons.frame("thinking", 0, 24)  # cached object


def test_png_bytes_round_trip(icons):
    import io
    data = icons.png("typing", 2, 48)
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    assert im.size == (48, 48)
    assert im.tobytes() == icons.frame("typing", 2, 48).tobytes()


# --- the variants ---------------------------------------------------------------------


def test_greyed_variant_differs_is_grey_and_translucent(icons):
    idle, grey = icons.frame("idle", 0, 32), icons.frame("grey", 0, 32)
    assert idle.tobytes() != grey.tobytes()
    px = [p for p in pixels(grey) if p[3] > 0]
    assert px and all(r == g == b for r, g, b, _a in px), "greyed Clawd must be desaturated"
    assert max(a for *_rgb, a in px) <= 153, "60 % alpha at most"
    assert opaque(grey) == opaque(idle), "same silhouette as the idle sprite"


def test_error_variant_is_clawd_with_a_red_dot_in_the_corner(icons):
    idle, err = icons.frame("idle", 0, 32), icons.frame("error", 0, 32)
    assert idle.tobytes() != err.tobytes()
    r, g, b, a = err.getpixel((32 - 6, 32 - 6))
    assert a == 255 and r > 200 and g < 80 and b < 80, "a red dot, bottom right"
    # the rest of Clawd is still there
    assert err.getpixel((16, 12)) == idle.getpixel((16, 12)) or opaque(err) >= opaque(idle)
    # also at the smallest size
    r, g, b, a = icons.frame("error", 0, 16).getpixel((12, 12))
    assert a == 255 and r > 200 and g < 80


def test_idle_is_the_orange_clawd(icons):
    px = [p for p in pixels(icons.frame("idle", 0, 64)) if p[3] == 255]
    r, g, b, _ = max(set(px), key=px.count)  # the body colour
    assert r > 180 and 90 < g < 150 and b < 120


# --- from the C headers ----------------------------------------------------------------


def test_decoder_reads_the_real_idle_header():
    sp = make_icons.load_sprite("idle", HEADERS)
    assert (sp.width, sp.height, sp.frame_count) == (72, 51, 96)
    frame = sp.frame(0)
    assert frame.getchannel("A").getbbox() == (6, 11, 66, 51)  # Clawd, nothing else
    assert frame.getpixel((0, 0))[3] == 0  # the 0x18C5 key is transparent
    # a body pixel is the RGB565 0xD40C expanded the way the firmware does
    assert make_icons.rgb565_to_rgb888(0xD40C) == (214, 130, 99)
    assert frame.getpixel((20, 20)) == (214, 130, 99, 255)


def test_every_sprite_the_app_shows_decodes_with_a_sound_header():
    for name in make_icons.KEEP:
        sp = make_icons.load_sprite(name, HEADERS)  # raises on a bad offset table
        assert sp.frame_count >= 1 and sp.frame(0).getchannel("A").getbbox()


def test_committed_assets_are_what_make_icons_produces(tmp_path):
    """Nobody hand-edited the PNGs, and the headers have not moved on since."""
    make_icons.write_assets(HEADERS, tmp_path)
    mine = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    theirs = {p.relative_to(ASSETS): p.read_bytes() for p in ASSETS.rglob("*") if p.is_file()}
    assert set(mine) == set(theirs)
    for rel in mine:
        assert mine[rel] == theirs[rel], f"{rel} is stale: run host/tray/make_icons.py"


def test_cropping_uses_one_box_per_sprite_so_frames_do_not_jump():
    sp = make_icons.load_sprite("sleeping", HEADERS)
    idxs = make_icons.pick_frames(sp.frame_count, 8)
    raw = [sp.frame(i) for i in idxs]
    box = make_icons.union_bbox(raw)
    squares = [make_icons.square_crop(f, box) for f in raw]
    assert len({s.size for s in squares}) == 1 and squares[0].width == squares[0].height


def test_pick_frames():
    assert make_icons.pick_frames(96, 1) == [0]
    assert make_icons.pick_frames(96, [21]) == [21]
    assert make_icons.pick_frames(12, 8) == [0, 1, 3, 4, 6, 7, 9, 10]
    assert make_icons.pick_frames(5, 8) == [0, 1, 2, 3, 4]  # cannot keep more than exist


# --- the .ico ----------------------------------------------------------------------------


def ico_sizes(path: Path):
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1)
    sizes = []
    for i in range(count):
        w, h = data[6 + 16 * i], data[7 + 16 * i]
        sizes.append((w or 256, h or 256))
    return sizes


def test_ico_has_every_size():
    assert ico_sizes(ASSETS / "clawd.ico") == [(s, s) for s in make_icons.ICO_SIZES]
    assert set(make_icons.ICO_SIZES) == {16, 20, 24, 32, 40, 48, 64, 256}
    with Image.open(ASSETS / "clawd.ico") as im:
        assert im.size == (256, 256)


# --- size choice ---------------------------------------------------------------------------


def test_nearest_size():
    assert [nearest_size(n) for n in (16, 20, 24, 32, 48, 64)] == [16, 20, 24, 32, 48, 64]
    assert nearest_size(17) == 20 and nearest_size(28) == 32
    assert nearest_size(500) == 64 and nearest_size(1) == 16


def test_tray_icon_size_is_one_of_the_built_sizes():
    assert icons_mod.tray_icon_size() in SIZES


# --- every state the daemon can report has a sprite ------------------------------------------------


def test_every_mapped_sprite_exists(icons):
    for sprite in ANIM_SPRITES.values():
        assert sprite in SPRITES and icons.frame_count(sprite) >= 1
    assert {"sleeping", "grey", "error"} <= set(SPRITES)
