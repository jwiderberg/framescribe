"""The visual stage on synthetic screen recordings with known events."""

import random

import cv2
import numpy as np
import pytest
from PIL import Image
from synth import (
    ARROW_BOX,
    H,
    W,
    analyse,
    crossfade,
    draw_arrow,
    encode,
    hold,
    page,
    scroll_frames,
    tall_document,
)

from framescribe.changedet import DetectParams, classify_change, estimate_scroll, is_scroll
from framescribe.cursor import CursorTracker
from framescribe.framestream import _select_expr, gray_frames, rgb_frames
from framescribe.models import Rect

SAMPLE = 0.5  # seconds between samples at the default 2 fps

A = page(1, header="Lighthouse")
B = page(2, bg=(20, 30, 70), fg=(235, 235, 245), header="Orchard")
C = page(3, bg=(215, 240, 220), fg=(20, 60, 30), header="Meadow", lines=7, size=18)


def keyframe(run_dir, state) -> np.ndarray:
    return np.asarray(Image.open(run_dir / state.keyframe_path).convert("RGB"))


def mean_diff(a, b) -> float:
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def assert_contiguous(art):
    assert art.states[0].start == 0
    for cur, nxt in zip(art.states, art.states[1:]):
        assert cur.end == nxt.start and cur.start < cur.end
    assert art.states[-1].end == pytest.approx(art.duration)
    for i, s in enumerate(art.states, start=1):
        assert s.id == i and s.start <= s.keyframe_time <= s.end


def test_page_switches_give_states_at_the_right_times(tmp_path):
    video = encode(hold(A, 4) + hold(B, 4) + hold(C, 4), tmp_path / "pages.mp4")
    art, ctx = analyse(video, tmp_path / "run")
    assert [round(s.start) for s in art.states] == [0, 4, 8]
    for s, truth in zip(art.states, (0, 4, 8)):
        assert abs(s.start - truth) <= SAMPLE
    assert [s.change.kind for s in art.states] == ["initial", "page", "page"]
    assert not art.cap.hit and all(s.settled for s in art.states)
    assert_contiguous(art)
    for s, pg in zip(art.states, (A, B, C)):
        assert mean_diff(keyframe(ctx.run_dir, s), pg) < 6
        assert s.keyframe_path.startswith("frames/s000") and s.keyframe_path.endswith(".jpg")
    assert art.states[1].keyframe_path == "frames/s0002_t00-00-03.jpg"  # state number, start time (3.75 s, truncated)
    assert art.content_rect == Rect(x=0, y=0, w=W, h=H) and art.fps_sampled == 2.0


def test_keyframes_are_native_resolution_jpegs(tmp_path):
    video = encode(hold(A, 3) + hold(B, 3), tmp_path / "pages.mp4")
    art, ctx = analyse(video, tmp_path / "run")
    im = Image.open(ctx.run_dir / art.states[0].keyframe_path)
    assert im.format == "JPEG" and im.size == (W, H)
    assert not list((ctx.run_dir / "frames").glob("*.part"))


@pytest.mark.parametrize("dy", [97, 140])
def test_scroll_of_known_distance_is_a_scroll_with_that_dy(tmp_path, dy):
    doc = tall_document(7)
    head = page(9, header="Glacier")[:34]
    frames = hold(doc[300 : 300 + H], 3) + scroll_frames(doc, 300, 300 + dy, 6, header=None) + hold(doc[300 + dy : 300 + dy + H], 4)
    frames = [f.copy() for f in frames]
    for f in frames:
        f[:34] = head  # a fixed header stays put while the content scrolls underneath
    video = encode(frames, tmp_path / "scroll.mp4")
    art, _ = analyse(video, tmp_path / "run")
    assert len(art.states) == 2
    change = art.states[1].change
    assert change.kind == "scroll"
    assert abs(change.scroll_dy + dy) <= 3  # content moved up by dy
    assert change.kind_confidence > 0.5 and change.changed_rect is not None
    assert abs(art.states[1].start - 3.0) <= 1.0


def test_page_switch_between_similar_sparse_pages_is_not_a_scroll(tmp_path):
    p1 = page(11, lines=6, header="Estuary", pitch=40)
    p2 = page(12, lines=6, header="Estuary", pitch=40)
    video = encode(hold(p1, 3) + hold(p2, 4), tmp_path / "sparse.mp4")
    art, _ = analyse(video, tmp_path / "run")
    assert len(art.states) == 2
    assert art.states[1].change.kind in ("partial", "page")
    assert art.states[1].change.scroll_dy is None


def test_sparse_pages_shifted_by_a_row_pitch_are_not_a_scroll():
    """Same template, different words, the second one starting one line lower: still not a scroll."""
    a = cv2.cvtColor(page(21, lines=8, pitch=30), cv2.COLOR_RGB2GRAY)
    b = cv2.cvtColor(page(22, lines=8, pitch=30, top=50), cv2.COLOR_RGB2GRAY)
    params = DetectParams()
    assert classify_change(a, b, params).kind != "scroll"
    assert not is_scroll(estimate_scroll(a, b, Rect(x=0, y=0, w=W, h=H)), params)


def test_real_scroll_between_arrays_is_found_with_exact_dy():
    doc = cv2.cvtColor(tall_document(5), cv2.COLOR_RGB2GRAY)
    a, b = doc[100 : 100 + H], doc[100 + 83 : 100 + 83 + H]
    ev = estimate_scroll(a, b, Rect(x=0, y=0, w=W, h=H))
    assert ev is not None and is_scroll(ev, DetectParams())
    assert ev.dy == -83 and ev.match > 0.95 and ev.pixel_agreement > 0.95
    c = classify_change(a, b, DetectParams())
    assert (c.kind, c.scroll_dy) == ("scroll", -83)


def test_toast_that_appears_and_disappears_leaves_localised_partial_changes(tmp_path):
    toast = A.copy()
    toast[300:350, 420:620] = (50, 50, 60)
    toast[315:325, 440:560] = (250, 250, 250)
    video = encode(hold(A, 3) + hold(toast, 4) + hold(A, 4), tmp_path / "toast.mp4")
    art, _ = analyse(video, tmp_path / "run")
    assert [round(s.start) for s in art.states] == [0, 3, 7]
    appear, vanish = art.states[1].change, art.states[2].change
    for c in (appear, vanish):
        assert c.kind in ("minor", "partial") and 0.02 < c.changed_fraction < 0.1
        r = c.changed_rect
        assert r is not None and r.x >= 410 and r.y >= 290 and r.x1 <= 630 and r.y1 <= 360
    assert mean_diff(keyframe(tmp_path / "run", art.states[2]), A) < 6


def test_flicker_that_returns_to_the_same_picture_is_not_a_state(tmp_path):
    flick = A.copy()
    flick[100:200, 100:300] = (200, 0, 0)
    video = encode(hold(A, 3) + hold(flick, 0.5) + hold(A, 4), tmp_path / "flicker.mp4")
    art, _ = analyse(video, tmp_path / "run")
    assert len(art.states) == 1


def path_points(n: int, seed: int = 4):
    """Pointer top-left positions: jumps and short moves, always changing between samples."""
    rng = random.Random(seed)
    x, y = 300, 200
    pts = []
    for _ in range(n):
        if rng.random() < 0.6:
            x, y = rng.randint(40, W - 60), rng.randint(60, H - 50)
        else:
            x, y = x + rng.choice((-1, 1)) * rng.randint(18, 40), y + rng.choice((-1, 1)) * rng.randint(18, 40)
            x, y = min(max(x, 20), W - 40), min(max(y, 40), H - 40)
        pts.append((x, y))
    return pts


def test_a_moving_pointer_alone_makes_no_state_and_is_tracked(tmp_path):
    pts = path_points(20)
    base = page(31, lines=9)
    frames = []
    for x, y in pts:
        for _ in range(int(SAMPLE * 10)):  # the pointer sits for one sample interval, then moves
            f = base.copy()
            draw_arrow(f, x, y)
            frames.append(f)
    video = encode(frames, tmp_path / "cursor.mp4")
    art, _ = analyse(video, tmp_path / "run")
    assert len(art.states) == 1
    samples = art.states[0].cursor
    assert len(samples) >= 10
    ok = 0
    for s in samples:
        # sample at time t describes the pointer as drawn in the frame at t
        px, py = pts[min(len(pts) - 1, int(s.t / SAMPLE))]
        centre = (px + ARROW_BOX[0] / 2, py + ARROW_BOX[1] / 2)
        ok += abs(s.x - centre[0]) <= 6 and abs(s.y - centre[1]) <= 6
        assert 0.5 <= s.confidence <= 1.0
    assert ok / len(samples) >= 0.8
    assert art.cursor_samples == len(samples)


def test_pointer_is_not_reported_when_the_page_changes_at_the_same_time(tmp_path):
    tracker = CursorTracker((H, W))
    a = cv2.cvtColor(A, cv2.COLOR_RGB2GRAY)
    b = cv2.cvtColor(B, cv2.COLOR_RGB2GRAY)
    assert tracker.feed(0.5, a, b, 0.9) is None and tracker.samples == []
    noise = a.copy()
    noise[::7, ::5] ^= 0x55  # scattered specks, not a pointer
    assert tracker.feed(1.0, a, noise, 0.003) is None


def test_static_video_is_exactly_one_state(tmp_path):
    video = encode(hold(A, 10), tmp_path / "static.mp4")
    art, _ = analyse(video, tmp_path / "run")
    assert len(art.states) == 1 and art.states[0].change.kind == "initial"
    assert art.states[0].start == 0 and art.states[0].end == pytest.approx(art.duration)
    assert not art.cap.hit and art.states[0].settled and art.states[0].cursor == []


def test_constantly_changing_video_engages_the_cap(tmp_path):
    rng = np.random.default_rng(3)
    frames = [rng.integers(0, 255, size=(H, W, 3), dtype=np.uint8) for _ in range(250)]  # 25 s of static noise
    video = encode(frames, tmp_path / "noise.mp4")
    art, _ = analyse(video, tmp_path / "run")
    assert art.cap.hit and art.cap.forced_closes >= 1
    assert len(art.states) <= 1 + int(art.duration / art.cap.max_event_seconds) + 1
    assert all(not s.settled for s in art.states[1:])
    assert_contiguous(art)


def test_state_rate_safeguard_limits_states_per_minute(tmp_path):
    pages = [A, B, C, page(4, bg=(250, 230, 200)), page(5, bg=(30, 30, 30), fg=(250, 250, 250))]
    frames = [f for i in range(16) for f in hold(pages[i % 5], 1.5)]  # a page switch every 1.5 s for 24 s
    video = encode(frames, tmp_path / "busy.mp4")
    art, _ = analyse(video, tmp_path / "run", visual_max_states_per_minute=6, visual_min_state_seconds=1.0)
    assert art.cap.hit and art.cap.suppressed_changes > 0
    assert len(art.states) <= 6 + 1
    free, _ = analyse(video, tmp_path / "run2", visual_max_states_per_minute=100, visual_min_state_seconds=1.0)
    assert len(free.states) > len(art.states) and not free.cap.hit


def test_keyframe_of_a_slow_fade_is_the_settled_picture_not_a_mid_fade_frame(tmp_path):
    video = encode(hold(A, 3) + crossfade(A, B, 4) + hold(B, 5), tmp_path / "fade.mp4")
    art, ctx = analyse(video, tmp_path / "run")
    assert len(art.states) == 2
    s = art.states[1]
    assert s.settled and s.keyframe_time >= 7.0  # the fade ends at 7 s
    assert mean_diff(keyframe(ctx.run_dir, s), B) < 6
    assert abs(s.start - 3.0) <= 1.0


def test_select_expression_is_balanced_and_merges_runs():
    assert _select_expr([5]) == "eq(n\\,5)"
    e = _select_expr([1, 2, 3, 7])
    assert e == "(between(n\\,1\\,3)+eq(n\\,7))"
    assert _select_expr(list(range(0, 6000, 2))).count("(") > 2999


def test_color_pass_returns_the_same_frames_the_gray_pass_numbered(tmp_path):
    video = encode(hold(A, 3) + hold(B, 3) + hold(C, 3), tmp_path / "pages.mp4")
    rect = Rect(x=0, y=0, w=W, h=H)
    gray = list(gray_frames(video, rect, 2.0))
    assert len(gray) == 18
    wanted = [0, 5, 7, 12, 17]
    got = dict(rgb_frames(video, rect, 2.0, wanted))
    assert sorted(got) == wanted
    for i in wanted:
        g = cv2.cvtColor(got[i], cv2.COLOR_RGB2GRAY)
        assert mean_diff(g, gray[i]) < 10
    # frames 5 and 7 straddle the first switch at 3 s: different pictures
    assert mean_diff(got[5], got[7]) > 20


def test_cursor_off_screen_edges_and_tiny_video_do_not_crash(tmp_path):
    video = encode(hold(A, 1), tmp_path / "short.mp4")
    art, _ = analyse(video, tmp_path / "run")
    assert len(art.states) == 1 and art.states[0].end == pytest.approx(art.duration)
