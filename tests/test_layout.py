import numpy as np
import pytest

from framescribe.ffmpeg import probe
from framescribe.layout import detect_layout, largest_rectangle, layout_from_ever

TOL = 4


def assert_rect(rect, x, y, w, h):
    # every edge within TOL (a tuple comparison is lexicographic: it passed whenever the first edge did)
    assert max(abs(rect.x - x), abs(rect.y - y), abs(rect.x1 - (x + w)), abs(rect.y1 - (y + h))) <= TOL, rect


@pytest.mark.parametrize("video", ["letterbox_video", "graybar_video"])
def test_letterbox_with_adjacent_tile(request, video):
    path = request.getfixturevalue(video)
    layout = detect_layout(path, probe(path), samples=8)
    assert (layout.frame_w, layout.frame_h) == (1280, 720)
    assert_rect(layout.content_rect, 0, 102, 960, 516)
    assert len(layout.other_rects) == 1
    assert_rect(layout.other_rects[0], 960, 270, 320, 180)
    assert layout.confidence > 0.9


def test_full_frame_video_is_all_content(fullframe_video):
    layout = detect_layout(fullframe_video, probe(fullframe_video), samples=8)
    assert_rect(layout.content_rect, 0, 0, 640, 360)
    assert layout.other_rects == []


def test_all_black_video_falls_back_to_the_full_frame_with_a_warning():
    layout = layout_from_ever(np.zeros((90, 160), np.uint8))
    assert (layout.content_rect.w, layout.content_rect.h) == (160, 90)
    assert "full-frame-fallback" in layout.method and layout.confidence == 0.0
    assert "entirely black" in layout.warning


def test_dark_sidebar_is_part_of_the_content():
    ever = np.full((200, 400), 17, np.uint8)
    ever[40:160, 0:300] = 23  # dark sidebar and page background
    ever[40:160, 80:300] = 200  # bright page
    layout = layout_from_ever(ever)
    assert_rect(layout.content_rect, 0, 40, 300, 120)


def test_largest_rectangle_on_l_shape():
    mask = np.zeros((10, 12), bool)
    mask[0:8, 0:8] = True
    mask[3:6, 8:12] = True
    r = largest_rectangle(mask)
    assert (r.x, r.y, r.w, r.h) == (0, 0, 8, 8)
    assert largest_rectangle(np.zeros((4, 4), bool)) is None


def ever_image(w=1280, h=720, bar=17):
    return np.full((h, w), bar, np.uint8)


@pytest.mark.parametrize("tile", [150, 200, 230])
def test_adjacent_tile_taller_than_half_the_content_is_not_absorbed_into_it(tile):
    ever = ever_image()
    ever[102:618, 0:960] = 200
    ever[200:520, 960:1280] = tile  # 320 rows: taller than half of the 516-row content, no gap to it
    layout = layout_from_ever(ever)
    assert_rect(layout.content_rect, 0, 102, 960, 516)  # earlier code widened the content to 972x516 for tile >= 200
    assert_rect(layout.other_rects[0], 960, 200, 320, 320)
    assert layout.warning is None


@pytest.mark.parametrize("band", [110, 150, 200])
@pytest.mark.parametrize("footer", [False, True])
def test_dark_slide_with_a_header_band_falls_back_instead_of_returning_the_band(band, footer):
    ever = ever_image(bar=0)
    ever[0:band, :] = 160  # coloured title band
    ever[300:500, 100:700] = 200  # a picture on the black slide
    if footer:
        ever[660:720, :] = 90
    layout = layout_from_ever(ever)
    assert (layout.content_rect.w, layout.content_rect.h) == (1280, 720)  # before: 0,0 1280x110 at confidence 0.61
    assert layout.confidence < 0.5 and "full-frame-fallback" in layout.method
    assert f"1280x{band}" in layout.warning and "too small" in layout.warning and "--content-rect" in layout.warning


def test_full_frame_slide_on_black_does_not_pass_for_letterboxed_content():
    ever = ever_image(bar=0)
    ever[180:540, 320:960] = 220  # a bright picture floating on a black slide
    layout = layout_from_ever(ever)
    assert (layout.content_rect.x, layout.content_rect.y, layout.content_rect.w, layout.content_rect.h) == (0, 0, 1280, 720)
    assert layout.confidence < 0.5 and "full-frame-fallback" in layout.method
    assert "not confident" in layout.warning and "--content-rect" in layout.warning


def test_full_frame_terminal_with_sparse_text_falls_back_instead_of_returning_a_strip():
    ever = ever_image(bar=0)
    rng = np.random.default_rng(3)
    for row in range(40, 700, 22):  # lines of text: short bright dashes
        for x in range(30, 900, 9):
            if rng.random() < 0.7:
                ever[row : row + 10, x : x + 6] = 230
    layout = layout_from_ever(ever)
    assert (layout.content_rect.w, layout.content_rect.h) == (1280, 720)  # was an 886x10 strip at confidence 0.02
    assert layout.warning and layout.confidence < 0.5
