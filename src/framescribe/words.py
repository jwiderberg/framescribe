"""Words from backend output, and sentence segmentation. Shared by every ASR backend."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Protocol

from .errors import WordOrderError
from .models import Segment, Word

PAUSE_SPLIT_SECONDS = 2.0
MAX_WORD_SECONDS = 3.0  # a longer "word" is a timing artefact, not speech
EPSILON = 1e-3
PAST_END_SECONDS = 0.5  # a word starting this little after the audio end is moved to the end
MAX_PAST_END_WORDS = 5  # words starting later are dropped (and logged), but more than this many is an error
_SENTENCE_END = re.compile(r"[.?!…]+[\"')\]]*$")
_ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.", "vs.", "etc.", "e.g.", "i.e.", "no."}
_LEADING_PUNCT = ",.;:!?%)"


class Token(Protocol):
    text: str
    start: float
    end: float
    confidence: float


def _is_punctuation(text: str) -> bool:
    return not any(ch.isalnum() for ch in text)


def words_from_tokens(groups: Iterable[Sequence[Token]]) -> list[Word]:
    """Build words from sentencepiece tokens: a token starting with a space starts a new word.

    The first token of each group (a sentence) always starts a new word. A punctuation-only token
    adds its text but not its time: the model emits it after the pause that follows the word, so
    using its end would stretch the word across the silence.
    """
    words: list[Word] = []
    for group in groups:
        cur: Word | None = None
        for t in group:
            if cur is None or t.text.startswith(" "):
                if cur:
                    words.append(cur)
                cur = Word(text=t.text.strip(), start=t.start, end=t.end, conf=t.confidence)
            else:
                cur.text += t.text
                if not _is_punctuation(t.text):
                    cur.end = t.end
                    cur.conf = min(cur.conf if cur.conf is not None else 1.0, t.confidence)
        if cur:
            words.append(cur)
    return [w for w in words if w.text]


def attach_leading_punctuation(words: list[Word]) -> list[Word]:
    """Join words that start with punctuation (e.g. a stray ",000" after "$100") to the previous word.

    As in `words_from_tokens`, a punctuation-only fragment does not extend the previous word's end.
    """
    out: list[Word] = []
    for w in words:
        if out and w.text and w.text[0] in _LEADING_PUNCT:
            prev = out[-1]
            prev.text += w.text
            if not _is_punctuation(w.text):
                prev.end = max(prev.end, w.end)
        else:
            out.append(w)
    return out


def finalize_words(words: list[Word], duration: float, dropped: list[Word] | None = None) -> list[Word]:
    """Check backend word timings and tidy their ends. Out-of-order or out-of-range words raise.

    Starts must not decrease and must lie in [0, duration]; a word must not end before it starts. A word that starts
    up to PAST_END_SECONDS after `duration` is moved to the end. One that starts later is past the audio: with a
    `dropped` list it is left out and appended there (more than MAX_PAST_END_WORDS of them raise), without one it
    raises. Tidying touches ends only: a word is capped at MAX_WORD_SECONDS, may not end after `duration` (up to a
    one-second overshoot is clamped), and is cut off where the next word starts.
    """
    out: list[Word] = []
    past: list[Word] = []
    for i, w in enumerate(words):
        if w.end < w.start - EPSILON:
            raise WordOrderError(f"word {i} {w.text!r} ends ({w.end:.3f}) before it starts ({w.start:.3f})")
        if w.start < -EPSILON:
            raise WordOrderError(f"word {i} {w.text!r} starts at {w.start:.3f}, outside [0, {duration:.3f}]")
        if w.start > duration + PAST_END_SECONDS:
            if dropped is None:
                raise WordOrderError(f"word {i} {w.text!r} starts at {w.start:.3f}, outside [0, {duration:.3f}]")
            past.append(w)
            continue
        if out and w.start < out[-1].start - EPSILON:
            raise WordOrderError(
                f"word {i} {w.text!r} starts at {w.start:.3f}, before the previous word {out[-1].text!r} at {out[-1].start:.3f}"
            )
        if w.end > duration + 1.0 and w.start <= duration:
            raise WordOrderError(f"word {i} {w.text!r} ends at {w.end:.3f}, past the audio end {duration:.3f}")
        start = min(max(w.start, 0.0, out[-1].start if out else 0.0), duration)
        end = min(max(w.end, start), duration, start + MAX_WORD_SECONDS)
        out.append(Word(text=w.text, start=start, end=end, conf=w.conf))
    if len(past) > MAX_PAST_END_WORDS:
        raise WordOrderError(f"{len(past)} words start more than {PAST_END_SECONDS:g} s after the audio end "
                             f"{duration:.3f} (the first at {past[0].start:.3f}); at most {MAX_PAST_END_WORDS} are dropped")
    if dropped is not None:
        dropped.extend(past)
    for cur, nxt in zip(out, out[1:]):
        cur.end = min(cur.end, nxt.start)
    return out


def _ends_sentence(text: str) -> bool:
    if not _SENTENCE_END.search(text) or text.lower() in _ABBREVIATIONS:
        return False
    return not re.fullmatch(r"[A-Z]\.", text)  # an initial such as "J."


def segment_sentences(words: Sequence[Word]) -> list[Segment]:
    """Split words into sentences at terminal punctuation or a long pause."""
    segments: list[Segment] = []
    first = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        pause = not last and words[i + 1].start - w.end > PAUSE_SPLIT_SECONDS
        if last or pause or _ends_sentence(w.text):
            chunk = words[first : i + 1]
            segments.append(
                Segment(
                    start=chunk[0].start,
                    end=chunk[-1].end,
                    text=" ".join(c.text for c in chunk),
                    word_start_idx=first,
                    word_end_idx=i + 1,
                )
            )
            first = i + 1
    return segments
