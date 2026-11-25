"""The state detector on synthetic gray frames: the per-minute cap and the start time of a state."""

import numpy as np
import pytest

from framescribe.changedet import DetectParams, StateDetector

FPS = 2.0


def picture(i: int, h: int = 90, w: int = 160) -> np.ndarray:
    """A dark frame with a few bright blocks, different for every i."""
    rng = np.random.default_rng(i)
    f = np.full((h, w), 30, np.uint8)
    for _ in range(6):
        y, x = rng.integers(0, h - 20), rng.integers(0, w - 30)
        f[y : y + 20, x : x + 30] = rng.integers(120, 255)
    return f


def detect(frames, **params) -> StateDetector:
    d = StateDetector(DetectParams(fps=FPS, **params))
    for f in frames:
        d.feed(f)
    d.finish()
    return d


def scheduled(changes: list[float], seconds: float):
    """Picture i is shown from changes[i] on."""
    cur = 0
    for k in range(int(seconds * FPS)):
        while cur + 1 < len(changes) and k / FPS >= changes[cur + 1]:
            cur += 1
        yield picture(cur)


def test_state_rate_cap_acts_only_in_the_busy_minute_and_lets_go_afterwards():
    burst = [2.0 * i for i in range(1, 46)]  # a new picture every 2 s up to 90 s: 30 a minute
    later = [90.0 + 20.0 * i for i in range(1, 16)]  # then quiet, a real change every 20 s up to 390 s
    d = detect(scheduled([0.0, *burst, *later], 400.0))
    starts = [s.start for s in d.states]
    assert d.cap.suppressed > 0  # the burst was capped ...
    for t in later:  # ... but every change after it is a state, at its own time
        assert any(abs(s - t) <= 0.5 for s in starts), (t, starts)
    for m in range(0, 400, 10):  # no minute holds more than the cap
        assert sum(1 for s in starts if m <= s < m + 60) <= 20 + 1


def test_after_an_animation_that_never_settles_a_real_change_starts_when_it_appeared():
    rng = np.random.default_rng(5)
    noise = [rng.integers(0, 255, size=(90, 160), dtype=np.uint8) for _ in range(int(35 * FPS))]  # 0-35 s
    still = [picture(99)] * int(10 * FPS)  # 35-45 s
    d = detect(noise + still)
    assert d.cap.forced_closes >= 2
    last = d.states[-1]
    assert last.settled and last.settled_time >= 35.0
    assert last.start == pytest.approx(35.0, abs=0.5)  # not the start of the animation's last cut-off piece (~31 s)


def test_a_fade_still_starts_when_it_began():
    a, b = picture(1), picture(2)
    fade = [((1 - x) * a + x * b).astype(np.uint8) for x in np.linspace(0, 1, 9)[1:]]  # 4 s at 2 fps
    d = detect([a] * 6 + fade + [b] * 10)
    assert len(d.states) == 2 and d.states[1].start == pytest.approx(3.0, abs=0.6)


def test_a_suppressed_change_that_stays_on_screen_is_kept_later_with_the_time_it_appeared():
    # A at 0, B at 10 s, C at 12.5 s: C comes too soon after B and is suppressed. It stays on screen, so it becomes a
    # state once the minimum time has passed, stamped with the time it appeared (it used to be suppressed forever).
    d = detect(scheduled([0.0, 10.0, 12.5], 30.0), min_state_seconds=4.0)
    assert d.cap.suppressed >= 1
    assert [s.start for s in d.states] == pytest.approx([0.0, 9.75, 12.25], abs=0.01)
    assert d.states[-1].settled_time >= 12.5
