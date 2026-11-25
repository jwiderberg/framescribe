from dataclasses import dataclass

import pytest

from framescribe.errors import WordOrderError
from framescribe.models import Word
from framescribe.words import (
    MAX_WORD_SECONDS,
    attach_leading_punctuation,
    finalize_words,
    segment_sentences,
    words_from_tokens,
)


@dataclass
class Tok:
    text: str
    start: float
    end: float
    confidence: float = 1.0


def w(text, start, end):
    return Word(text=text, start=start, end=end)


def test_tokens_to_words_space_starts_a_word():
    sentence = [Tok(" Mar", 0.0, 0.2, 0.9), Tok("ble", 0.2, 0.4, 0.5), Tok(" Harbour", 0.5, 0.9), Tok(".", 0.9, 1.0, 0.8)]
    words = words_from_tokens([sentence])
    assert [(x.text, x.start, x.end, x.conf) for x in words] == [("Mar" + "ble", 0.0, 0.4, 0.5), ("Harbour.", 0.5, 0.9, 1.0)]  # the "." token adds text, not time


def test_first_token_of_each_sentence_starts_a_word():
    a = [Tok(" Hi.", 0, 0.5)]
    b = [Tok("There.", 0.6, 1.0)]  # no leading space, but a new sentence
    assert [x.text for x in words_from_tokens([a, b])] == ["Hi.", "There."]


def test_stray_punctuation_word_is_attached():
    words = attach_leading_punctuation([w("$100", 0, 1), w(",000", 1, 1.2), w("dollars.", 1.3, 2)])
    assert [(x.text, x.end) for x in words] == [("$100,000", 1.2), ("dollars.", 2)]


def test_finalize_trims_overlap_caps_duration_and_keeps_order():
    words = finalize_words([w("a", 0.0, 1.5), w("b", 1.0, 2.0), w("c", 1.8, 9.0)], duration=10.0)
    assert [(x.start, x.end) for x in words] == [(0.0, 1.0), (1.0, 1.8), (1.8, 1.8 + MAX_WORD_SECONDS)]


def test_finalize_raises_instead_of_clamping_out_of_order_words():
    # the old normalisation turned this into two words at 31.6 s; disorder must now be loud
    with pytest.raises(WordOrderError, match="before the previous word"):
        finalize_words([w("Right.", 31.62, 31.98), w("Right.", 18.58, 19.02)], duration=100.0)


@pytest.mark.parametrize("bad", [w("x", 5.0, 4.0), w("x", -2.0, 1.0), w("x", 120.0, 121.0), w("x", 1.0, 150.0)])
def test_finalize_raises_on_impossible_timings(bad):
    with pytest.raises(WordOrderError):
        finalize_words([bad], duration=100.0)


def test_late_punctuation_token_does_not_stretch_the_word():
    # "on" ends at 1.0, the "." token is emitted after a 19 s pause (parakeet does this)
    sentence = [Tok(" on", 0.6, 1.0), Tok(".", 20.0, 20.3), Tok(" Next", 20.5, 20.9)]
    on, nxt = words_from_tokens([sentence])
    assert (on.text, on.start, on.end) == ("on.", 0.6, 1.0)
    assert (nxt.start, nxt.end) == (20.5, 20.9)


def test_stray_punctuation_fragment_does_not_extend_previous_word_but_digits_do():
    words = attach_leading_punctuation([w("on", 0, 1), w(".", 15, 15.2), w("$100", 16, 17), w(",000", 17, 17.4)])
    assert [(x.text, x.end) for x in words] == [("on.", 1), ("$100,000", 17.4)]


def test_implausibly_long_word_is_capped():
    (word,) = finalize_words([w("hmm", 1.0, 40.0)], duration=100.0)
    assert word.end == 1.0 + MAX_WORD_SECONDS


def test_sentences_split_on_terminal_punctuation_with_indices():
    words = [w("Hello", 0, 0.4), w("there.", 0.4, 0.9), w("Is", 1.0, 1.2), w("it", 1.2, 1.3), w("(ok?)", 1.3, 1.8), w("Yes", 2.0, 2.3)]
    segs = segment_sentences(words)
    assert [(s.text, s.word_start_idx, s.word_end_idx) for s in segs] == [
        ("Hello there.", 0, 2),
        ("Is it (ok?)", 2, 5),
        ("Yes", 5, 6),
    ]
    assert (segs[1].start, segs[1].end) == (1.0, 1.8)


def test_abbreviations_and_initials_do_not_end_a_sentence():
    words = [w("Dr.", 0, 0.2), w("J.", 0.2, 0.4), w("Smith", 0.4, 0.8), w("e.g.", 0.8, 1.0), w("agrees.", 1.0, 1.5)]
    assert [s.text for s in segment_sentences(words)] == ["Dr. J. Smith e.g. agrees."]


def test_long_pause_splits_without_punctuation():
    words = [w("so", 0, 0.3), w("well", 0.3, 0.6), w("anyway", 3.5, 4.0)]
    assert [s.text for s in segment_sentences(words)] == ["so well", "anyway"]


def test_every_word_is_in_exactly_one_segment():
    words = [w(t, i * 0.5, i * 0.5 + 0.4) for i, t in enumerate("a b. c d? e".split())]
    covered = [i for s in segment_sentences(words) for i in range(s.word_start_idx, s.word_end_idx)]
    assert covered == list(range(len(words)))


@pytest.mark.parametrize("text", ["Wait...", "Really?!", 'He said "go."'])
def test_other_terminal_forms_end_a_sentence(text):
    assert len(segment_sentences([w(text, 0, 1), w("Next", 1, 2)])) == 2


def test_words_past_the_end_are_clamped_within_half_a_second_and_dropped_beyond_it():
    kept = finalize_words([w("a", 9.0, 9.9), w("b", 9.95, 10.3), w("c", 10.4, 10.6)], duration=10.0, dropped=[])
    assert [(x.text, x.start, x.end) for x in kept] == [("a", 9.0, 9.9), ("b", 9.95, 10.0), ("c", 10.0, 10.0)]
    dropped: list[Word] = []
    kept = finalize_words([w("a", 9.0, 9.5), w("late", 10.6, 11.0)], duration=10.0, dropped=dropped)
    assert [x.text for x in kept] == ["a"] and [x.text for x in dropped] == ["late"]
    with pytest.raises(WordOrderError, match="at most 5"):
        finalize_words([w(f"x{i}", 11.0 + i, 11.5 + i) for i in range(6)], duration=10.0, dropped=[])
    with pytest.raises(WordOrderError, match="outside"):  # without a list to drop into, still an error
        finalize_words([w("late", 10.6, 11.0)], duration=10.0)
