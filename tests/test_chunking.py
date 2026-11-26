import numpy as np
import pytest

from framescribe import chunking
from framescribe.chunking import Levels, read_wav, write_wav
from framescribe.models import Word
from framescribe.stages.asr_parakeet import BARE_MIN_WORDS, is_bare, plan_cuts, word_boundary_cut

RATE = 16000


def noise(seconds, seed=0):
    return np.random.default_rng(seed).normal(0, 3000, int(seconds * RATE)).astype(np.int16)


def silence(x, start, end):
    x[int(start * RATE) : int(end * RATE)] = 0


def cuts_of(x, target, search):
    return plan_cuts(Levels(x, RATE), len(x) / RATE, target, search)


def test_cut_goes_to_the_centre_of_the_longest_pause_not_the_quiet_spot_nearest_the_target():
    # A short pause right at the target and a long one farther away: before, ties in quietness went to the one
    # nearest the target, so speech started ~0.1 s after the cut (6 of the 7 chunks that lost punctuation).
    x = noise(100)
    silence(x, 39.9, 40.15)  # 0.25 s at the target
    silence(x, 33.0, 34.2)  # 1.2 s, 6.4 s away
    cut = cuts_of(x, target=40, search=10)[0]
    assert cut.kind == "pause" and cut.time == pytest.approx(33.6, abs=0.02)
    assert cut.pause_seconds == pytest.approx(1.2, abs=0.05) and cut.level_db < -100


def test_equally_long_pauses_go_to_the_one_nearest_the_target():
    x = noise(100)
    silence(x, 31.0, 31.5)
    silence(x, 39.0, 39.5)
    assert cuts_of(x, target=40, search=10)[0].time == pytest.approx(39.25, abs=0.02)


def test_pause_threshold_is_relative_to_the_recording_so_a_noise_bed_still_has_pauses():
    x = noise(100)
    bed = np.random.default_rng(9).normal(0, 300, len(x))  # 20 dB under the "speech"
    x = x.astype(np.float64)
    x[int(37.0 * RATE) : int(37.8 * RATE)] = 0
    x = np.clip(x + bed, -32768, 32767).astype(np.int16)
    cut = cuts_of(x, target=40, search=10)[0]
    assert cut.kind == "pause" and cut.time == pytest.approx(37.4, abs=0.03)


def test_without_a_pause_the_search_widens_then_falls_back_to_a_no_pause_cut():
    x = noise(200, seed=1)
    silence(x, 59.0, 59.6)  # outside 40 +- 10, inside 40 +- 20
    (cut, *_) = cuts_of(x, target=40, search=10)
    assert cut.kind == "pause" and cut.time == pytest.approx(59.3, abs=0.02)
    y = noise(200, seed=2)
    silence(y, 80.0, 80.1)  # a gap shorter than MIN_PAUSE_SECONDS is not a pause
    (cut, *_) = cuts_of(y, target=40, search=10)
    assert cut.kind == "no_pause" and cut.time == 40.0 and cut.level_db > -40


def test_chunks_follow_each_other_and_cuts_lie_in_pauses():
    x = noise(500, seed=1)
    for t in range(30, 500, 30):
        silence(x, t, t + 0.4)
    cuts = cuts_of(x, target=100, search=20)
    edges = [0.0, *(c.time for c in cuts), 500.0]
    lengths = [b - a for a, b in zip(edges, edges[1:])]
    assert len(cuts) >= 4 and all(80 <= n <= 120 for n in lengths[:-1]) and lengths[-1] > 0
    assert all(c.kind == "pause" and any(abs(c.time - (t + 0.2)) <= 0.02 for t in range(30, 500, 30)) for c in cuts)


def test_audio_not_longer_than_target_plus_search_is_not_cut():
    assert cuts_of(noise(60), target=40, search=20) == []
    assert cuts_of(noise(61), target=40, search=20) != []


def test_longest_pause_measures_runs_in_full_and_respects_the_range():
    x = noise(30)
    silence(x, 20.0, 20.3)
    levels = Levels(x, RATE)
    pause = levels.longest_pause(10.0, 25.0, 15.0)
    assert pause.centre == pytest.approx(20.15, abs=0.02) and pause.seconds == pytest.approx(0.3, abs=0.05)
    assert levels.longest_pause(10.0, 19.0, 15.0) is None
    assert Levels(noise(30), RATE).longest_pause(10.0, 12.0, 11.0) is None
    assert chunking.MIN_PAUSE_SECONDS == 0.2


def w(text, start, end):
    return Word(text=text, start=start, end=end)


def test_no_pause_cut_is_in_the_widest_gap_between_words_near_the_target():
    words = [w("a", 36.0, 36.4), w("b", 36.45, 37.0), w("c", 37.2, 39.9), w("d", 39.95, 41.0), w("e", 41.02, 42.0)]
    cut = word_boundary_cut(words, 40.0, 5.0, Levels(noise(60), RATE))
    assert word_boundary_cut(words, 40.0, 0.5, Levels(noise(60), RATE)).time == pytest.approx(39.925)
    assert cut.kind == "no_pause" and cut.time == pytest.approx(37.1) and cut.pause_seconds == pytest.approx(0.2)


def test_bare_chunk_detection():
    plain = [w("word", i, i + 0.5) for i in range(40)]
    assert is_bare(plain)
    assert is_bare(plain[:BARE_MIN_WORDS])
    assert not is_bare(plain[: BARE_MIN_WORDS - 1])  # too few words to tell
    assert not is_bare([*plain[:38], w("end.", 38, 38.5), w("Next,", 39, 39.5)])


def test_wav_roundtrip(tmp_path):
    x = noise(1)
    write_wav(tmp_path / "a.wav", x, RATE)
    y, rate = read_wav(tmp_path / "a.wav")
    assert rate == RATE and (x == y).all()


def test_with_a_maximum_no_chunk_is_longer_and_the_widened_search_stays_inside_it():
    x = noise(200, seed=3)
    for t in (33.0, 60.0, 99.0, 141.0):
        silence(x, t, t + 0.3)
    cuts = plan_cuts(Levels(x, RATE), 200.0, target=30, search=5, maximum=40)
    # 33 and 60 lie within 30 +- 5 of the last cut; 99 only in the widened search (20 to 40 s after 60);
    # 141 is 42 s after 99, past the maximum, so there the cut is a no_pause one at the target
    assert [c.kind for c in cuts[:4]] == ["pause", "pause", "pause", "no_pause"]
    assert [c.time for c in cuts[:4]] == pytest.approx([33.15, 60.15, 99.15, 129.15], abs=0.03)
    edges = [0.0, *(c.time for c in cuts), 200.0]
    assert all(b - a <= 40.0 + 1e-9 for a, b in zip(edges, edges[1:]))


def test_no_pause_cut_without_two_words_near_it_stays_put_and_never_fails():
    """Sound without a pause and without words (music, noise): the decode has nothing to cut between."""
    levels = Levels(noise(60), RATE)
    for words in ([], [w("far", 2.0, 2.5), w("away", 2.6, 3.0)], [w("alone", 30.0, 30.4)]):
        cut = word_boundary_cut(words, 40.0, 5.0, levels)
        assert cut.kind == "no_pause" and cut.time == pytest.approx(40.0) and cut.pause_seconds == 0.0
    # one word across the cut: the cut goes to its nearer edge, so the word stays whole
    assert word_boundary_cut([w("long", 39.0, 40.2)], 40.0, 5.0, levels).time == pytest.approx(40.2)
    assert word_boundary_cut([w("long", 39.9, 41.5)], 40.0, 5.0, levels).time == pytest.approx(39.9)
