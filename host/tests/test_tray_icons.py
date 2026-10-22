"""The three tray icons: built from clawd-tank's sprite header, read from PNG at run time."""

import json
import struct
import sys
from pathlib import Path

import pytest
from PIL import Image

from clawd_tank_tray import icons as icons_mod
from clawd_tank_tray.icons import SIZES, IconSet, nearest_size
from clawd_tank_tray.state import ICON_NAMES

HOST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOST / "tray"))
import make_icons  # noqa: E402  (the build script, host/tray/make_icons.py)

ASSETS = HOST / "clawd_tank_tray" / "assets"
HEADERS = HOST.parent / "firmware" / "main" / "assets"


@pytest.fixture(scope="module")
def icons():
    return IconSet()


def pixels(img: Image.Image):
    """[(r, g, b, a), ...] of an RGBA image."""
    raw = img.convert("RGBA").tobytes()
    return list(zip(raw[0::4], raw[1::4], raw[2::4], raw[3::4]))


def visible(img: Image.Image):
    """pixels(), with every fully transparent pixel normalised to (0, 0, 0, 0): the
    colour under alpha 0 is not part of the picture."""
    return [(0, 0, 0, 0) if p[3] == 0 else p for p in pixels(img)]


def opaque(img: Image.Image) -> int:
    return sum(1 for a in img.getchannel("A").tobytes() if a > 0)


# --- sizes ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ICON_NAMES)
@pytest.mark.parametrize("size", SIZES)
def test_every_icon_has_a_square_non_empty_picture_at_every_size(icons, name, size):
    image = icons.icon(name, size)
    assert image.size == (size, size) and image.mode == "RGBA"
    assert opaque(image) > 0, f"{name} at {size}px is empty"


def test_the_same_picture_object_comes_back_so_one_handle_serves_a_state(icons):
    assert icons.icon("idle", 24) is icons.icon("idle", 24)
    assert icons.icon("idle", 24) is not icons.icon("grey", 24)
    assert len({id(icons.icon(n, 16)) for n in ICON_NAMES}) == 3


def test_an_unknown_icon_or_size_is_refused(icons):
    with pytest.raises(KeyError):
        icons.icon("thinking", 16)
    with pytest.raises(OSError):
        icons.icon("idle", 17)


def test_only_what_is_used_is_shipped():
    """The three pictures at the built sizes, the .ico and the manifest: no
    per-animation frames, no strips."""
    files = {p.relative_to(ASSETS).as_posix() for p in ASSETS.rglob("*") if p.is_file()}
    expected = {f"icons/{n}-{s}.png" for n in ICON_NAMES for s in SIZES}
    expected |= {"clawd.ico", "icons.json"}
    assert files == expected
    for png in (ASSETS / "icons").glob("*.png"):
        with Image.open(png) as im:
            assert im.width == im.height, f"{png.name} is a strip, not one picture"


def test_manifest_matches_the_assets():
    manifest = json.loads((ASSETS / "icons.json").read_text(encoding="utf-8"))
    assert manifest["sizes"] == list(SIZES)
    assert manifest["icons"] == list(ICON_NAMES)
    assert manifest["source"]["source"] == "sprite_idle.h"
    assert manifest["source"]["frame"] == 0


# --- the variants ---------------------------------------------------------------------


def test_greyed_variant_differs_is_grey_and_translucent(icons):
    idle, grey = icons.icon("idle", 32), icons.icon("grey", 32)
    assert idle.tobytes() != grey.tobytes()
    px = [p for p in pixels(grey) if p[3] > 0]
    assert px and all(r == g == b for r, g, b, _a in px), "greyed Clawd must be desaturated"
    assert max(a for *_rgb, a in px) <= 153, "60 % alpha at most"
    assert opaque(grey) == opaque(idle), "same silhouette as the idle sprite"


def test_error_variant_is_clawd_with_a_red_dot_in_the_corner(icons):
    idle, err = icons.icon("idle", 32), icons.icon("error", 32)
    assert idle.tobytes() != err.tobytes()
    r, g, b, a = err.getpixel((32 - 6, 32 - 6))
    assert a == 255 and r > 200 and g < 80 and b < 80, "a red dot, bottom right"
    # the rest of Clawd is still there
    assert err.getpixel((16, 12)) == idle.getpixel((16, 12)) or opaque(err) >= opaque(idle)
    # also at the smallest size
    r, g, b, a = icons.icon("error", 16).getpixel((12, 12))
    assert a == 255 and r > 200 and g < 80


def test_idle_is_the_orange_clawd(icons):
    px = [p for p in pixels(icons.icon("idle", 64)) if p[3] == 255]
    r, g, b, _ = max(set(px), key=px.count)  # the body colour
    assert r > 180 and 90 < g < 150 and b < 120


# --- from the C headers ----------------------------------------------------------------


SYNTHETIC_HEADER = """
#define TEST_WIDTH  4
#define TEST_HEIGHT 2
#define TEST_FRAME_COUNT 2
static const uint32_t test_frame_offsets[3] = {
    0, 4, 10,
};
static const uint16_t test_rle_data[] = {
    0x18C5,3, 0xD40C,5,
    0xD40C,2, 0x0000,1, 0x18C5,5,
};
"""


def test_decoder_is_exact_on_a_hand_made_header(tmp_path):
    """RLE (value, count) pairs, frame offsets with a sentinel, 0x18C5 transparent."""
    (tmp_path / "sprite_test.h").write_text(SYNTHETIC_HEADER, encoding="utf-8")
    sp = make_icons.load_sprite("test", tmp_path)
    assert (sp.width, sp.height, sp.frame_count) == (4, 2, 2)
    body = make_icons.rgb565_to_rgb888(0xD40C)
    assert body == (214, 130, 99)  # expanded the way rle_decode_argb8888() does
    f0 = visible(sp.frame(0))
    assert f0 == [(0, 0, 0, 0)] * 3 + [body + (255,)] * 5
    f1 = visible(sp.frame(1))
    assert f1 == [body + (255,)] * 2 + [(0, 0, 0, 255)] + [(0, 0, 0, 0)] * 5
    assert make_icons.rgb565_to_rgb888(0xFFFF) == (255, 255, 255)
    assert make_icons.rgb565_to_rgb888(0x0000) == (0, 0, 0)


def test_a_truncated_offset_table_is_refused(tmp_path):
    (tmp_path / "sprite_test.h").write_text(
        SYNTHETIC_HEADER.replace("0, 4, 10,", "0, 4,"), encoding="utf-8")
    with pytest.raises(ValueError):
        make_icons.load_sprite("test", tmp_path)


def test_decoder_agrees_with_the_real_header_it_reads():
    """Whatever art the headers hold today: the declared size, a transparent
    corner, and an independent decode of frame 0 byte for byte."""
    import re
    text = (HEADERS / "sprite_idle.h").read_text(encoding="utf-8")
    w, h, n = (int(re.search(rf"#define\s+IDLE_{k}\s+(\d+)", text).group(1))
               for k in ("WIDTH", "HEIGHT", "FRAME_COUNT"))
    sp = make_icons.load_sprite("idle", HEADERS)
    assert (sp.width, sp.height, sp.frame_count) == (w, h, n)

    expected = []  # a deliberately plain decode of frame 0
    i, end = sp.offsets[0], sp.offsets[1]
    while i < end:
        value, count = sp.data[i], sp.data[i + 1]
        rgba = (0, 0, 0, 0) if value == 0x18C5 else make_icons.rgb565_to_rgb888(value) + (255,)
        expected += [rgba] * count
        i += 2
    assert expected[: w * h] == visible(sp.frame(0))
    box = sp.frame(0).getchannel("A").getbbox()
    assert box and box != (0, 0, w, h)  # Clawd, with room around him
    assert pixels(sp.frame(0))[0][3] == 0


def test_the_sprite_the_icons_are_made_from_decodes_with_a_sound_header():
    sp = make_icons.load_sprite(make_icons.SOURCE, HEADERS)  # raises on a bad offset table
    assert sp.frame_count > make_icons.SOURCE_FRAME
    assert sp.frame(make_icons.SOURCE_FRAME).getchannel("A").getbbox()


def test_committed_assets_are_what_make_icons_produces(tmp_path):
    """Nobody hand-edited the PNGs, and the headers have not moved on since."""
    make_icons.write_assets(HEADERS, tmp_path)
    mine = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    theirs = {p.relative_to(ASSETS): p.read_bytes() for p in ASSETS.rglob("*") if p.is_file()}
    assert set(mine) == set(theirs)
    for rel in mine:
        assert mine[rel] == theirs[rel], f"{rel} is stale: run host/tray/make_icons.py"


def test_the_master_is_cropped_tight_and_square():
    master, info = make_icons.build_master(HEADERS)
    assert master.width == master.height == info["master"]
    x0, y0, x1, y1 = info["bbox"]
    assert max(x1 - x0, y1 - y0) == master.width      # no empty border left
    box = master.getchannel("A").getbbox()
    assert box[0] == 0 or box[1] == 0                  # touches the edge on its long side


def test_a_blank_frame_is_refused():
    with pytest.raises(ValueError):
        make_icons.bbox(Image.new("RGBA", (8, 8), (0, 0, 0, 0)))


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
