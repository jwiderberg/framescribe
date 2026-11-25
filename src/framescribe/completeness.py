"""Completeness: phrases another source heard where the primary transcript has nothing, and filling them in, marked.

Pure functions, no I/O. The primary word list is never altered: filled words are added between primary words,
each carrying its source and a `filled` marker.

How a gap is found, per other source (a second decode, another engine, embedded captions, an imported transcript):

1. Both word lists are normalised to comparison tokens (case and punctuation dropped, common contractions,
   colloquial forms and numbers spelled out, filler sounds removed), so "gonna" and "going to" or "10" and "ten"
   compare equal.
2. The token sequences are aligned with an edit-distance alignment that may only pair tokens close in time
   (TIME_TOLERANCE seconds; for captions, which only have cue-level times, the cue's span plus the tolerance).
   Equal tokens paired this way are anchors.
3. A run of the other source's tokens without an anchor is a gap only where the primary has no words at that time:
   word-timed sources are shifted by the local offset of the surrounding anchors and kept where no primary word
   covers them; cue-level sources only where the primary has no words at all between the two anchors. The primary
   must also leave enough silence there for the phrase (MIN_GAP_SILENCE, GAP_SILENCE_SHARE), so a word that one
   engine heard and the other merged into a neighbour is a wording difference, not a gap.

Gaps of different sources at the same place (in the same primary silence and within CLUSTER_SECONDS, or the same
phrase anywhere in that silence) are one gap. It is `confirmed` when two or more sources have it and every word to be
filled (normalised) is in at least two sources within the time tolerance of the fill (TIME_TOLERANCE, for
captions CUE_TOLERANCE beyond the cue), else `unconfirmed`. A gap is `rejected` and
not filled when its words only repeat words of the primary words right next to it (`repeats-adjacent`), when it comes from one
source and is only single letters or sounds that are no word (`fragment`), or when it comes from WhisperKit alone in
a span without speech energy (`no-speech`: Whisper's habit of inventing text in silence).
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .models import Caption, Completeness, Gap, TranscriptWord, Word

TIME_TOLERANCE = 3.0  # seconds: two tokens further apart than this never pair ...
CUE_TOLERANCE = 5.0  # ... and for a caption, this far beyond its cue's span (captions lag behind the speech)
BAND_SECONDS = 12.0  # the alignment only considers primary tokens this close to the other source's token
COVER_TOLERANCE = 0.1  # a primary word covers a moment this close to it
WORD_MARGIN = 0.3  # a primary word this close to an unanchored run of a word-timed source is "there" ...
CUE_MARGIN = 1.0  # ... and this close for a cue-level source
MIN_GAP_SILENCE = 0.3  # seconds of primary silence needed for any gap
GAP_SILENCE_SHARE = 0.5  # ... and at least this share of the phrase's own duration
CUE_SECONDS_PER_WORD = 0.15  # cue-level sources: primary silence needed per filled word
MIN_SPEECH_SHARE = 0.1  # a span with less of its 10 ms frames above the pause threshold has no speech energy
MIN_SPEECH_SECONDS = 0.15  # ... or less than this in total
ENERGY_MARGIN = 0.2  # seconds around a gap's words in which its speech energy is measured
CLUSTER_SECONDS = 1.0  # gaps of different sources this close (in the same primary silence) are one gap ...
ADJACENT_WORDS = 3  # a gap of only words of this many primary words on either side of it is rejected
SAME_PHRASE_SHARE = 0.6  # ... and so are two that share this much of the shorter one's tokens, anywhere in it
SOURCE_PRIORITY = ("parakeet-pass2", "whisperkit", "captions", "import")
HALLUCINATION_PRONE = frozenset({"whisperkit"})

FILLERS = frozenset({"uh", "um", "uhm", "umm", "er", "erm", "ah", "eh", "hmm", "hm", "mm", "mhm", "mmhmm", "uhhuh"})
_REPLACE = {
    "gonna": "going to", "wanna": "want to", "gotta": "got to", "kinda": "kind of", "sorta": "sort of",
    "lemme": "let me", "gimme": "give me", "dunno": "do not know", "ok": "okay", "alright": "all right",
    "yeah": "yes", "yep": "yes", "yup": "yes", "ya": "you", "cause": "because", "cos": "because", "cuz": "because",
    "won't": "will not", "can't": "can not", "cannot": "can not", "shan't": "shall not", "ain't": "is not",
    "let's": "let us", "it's": "it is", "that's": "that is", "what's": "what is", "there's": "there is",
    "here's": "here is", "he's": "he is", "she's": "she is", "who's": "who is", "where's": "where is",
    "how's": "how is", "y'all": "you all", "'cause": "because", "percent": "percent", "%": "percent",
}
_SUFFIXES = (("n't", " not"), ("'re", " are"), ("'ve", " have"), ("'ll", " will"), ("'m", " am"), ("'d", " would"))
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
        "seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORDINAL_LAST = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
                 "nine": "ninth", "twelve": "twelfth"}
_ORDINAL = re.compile(r"([0-9]+)(?:st|nd|rd|th)")
_PIECES = re.compile(r"[0-9]+(?:st|nd|rd|th)(?![^\W_])|[0-9][0-9,]*(?:\.[0-9]+)?%?|[^\W_]+(?:'[^\W_]+)*|%")
_VOWELS = frozenset("aeiouy")


def number_words(n: int) -> str:
    """0 to 999,999,999 in words, without "and": 1500 -> "one thousand five hundred"."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        return _ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + number_words(n % 100))
    for size, name in ((1_000_000, "million"), (1000, "thousand")):
        if n >= size:
            return number_words(n // size) + " " + name + ("" if n % size == 0 else " " + number_words(n % size))
    raise ValueError(n)


def ordinal_words(n: int) -> str:
    """3 -> "third", 21 -> "twenty first", 40 -> "fortieth"."""
    *head, last = number_words(n).split()
    if last in _ORDINAL_LAST:
        last = _ORDINAL_LAST[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return " ".join([*head, last])


def _number(piece: str) -> str:
    percent = piece.endswith("%")
    digits = piece.rstrip("%").replace(",", "")
    whole, _, frac = digits.partition(".")
    if len(whole) > 9:
        return piece
    text = number_words(int(whole))
    if frac:
        text += " point " + " ".join(_ONES[int(d)] for d in frac)
    return text + (" percent" if percent else "")


def normalise(text: str) -> list[str]:
    """Comparison tokens of one word as an engine wrote it (it can give none, one or several)."""
    text = text.lower().replace("’", "'").replace("‘", "'")
    out: list[str] = []
    for piece in _PIECES.findall(text):
        ordinal = _ORDINAL.fullmatch(piece)
        if ordinal and len(ordinal.group(1)) <= 9:
            piece = ordinal_words(int(ordinal.group(1)))
        elif piece[0].isdigit():
            piece = _number(piece)
        piece = _REPLACE.get(piece, piece)
        for suffix, repl in _SUFFIXES:
            if piece.endswith(suffix) and len(piece) > len(suffix):
                piece = piece[: -len(suffix)] + repl
                break
        if piece.endswith("'s"):
            piece = piece[:-2] + "s"  # possessive or "is": the same on both sides is all that matters
        for tok in piece.replace("'", "").split():
            if tok and tok not in FILLERS:
                out.append(tok)
    return out


# ---- sources --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """Words of one other source. `cue_level`: the times are a caption cue's span, not the word's own."""

    name: str
    words: Sequence[Word]
    cue_level: bool = False


def caption_source(captions: Sequence[Caption], name: str = "captions") -> Source:
    """Captions as a cue-level source: every word of a cue carries the cue's start and end."""
    words = [Word(text=t, start=c.start, end=c.end) for c in sorted(captions, key=lambda c: c.start)
             for t in c.text.split()]
    return Source(name=name, words=words, cue_level=True)


@dataclass(frozen=True)
class _Tok:
    text: str
    word: int  # index of the word it came from
    t: float  # position in time (a word's start; for a cue-level word, its share of the cue)
    lo: float  # earliest time it can have been said
    hi: float  # latest


def _tokens(words: Sequence[Word], cue_level: bool) -> list[_Tok]:
    toks: list[_Tok] = []
    if not cue_level:
        for i, w in enumerate(words):
            toks += [_Tok(n, i, w.start, w.start, w.end) for n in normalise(w.text)]
        return toks
    i = 0
    while i < len(words):  # one cue: consecutive words with the same span
        j = i
        while j < len(words) and (words[j].start, words[j].end) == (words[i].start, words[i].end):
            j += 1
        cue = [(k, n) for k in range(i, j) for n in normalise(words[k].text)]
        lo, hi = words[i].start, words[i].end
        for q, (k, n) in enumerate(cue):
            toks.append(_Tok(n, k, lo + (hi - lo) * (q + 0.5) / len(cue), lo, hi))
        i = j
    return toks


# ---- alignment ------------------------------------------------------------------------------------------------


def align(other: Sequence[_Tok], primary: Sequence[_Tok], tolerance: float = TIME_TOLERANCE) -> list[int | None]:
    """For every token of `other`, the index of the primary token it is anchored to, or None.

    Edit-distance alignment (match 0, substitution 1, insertion and deletion 1) in a band of BAND_SECONDS around each
    token; primary tokens before the first and after the last token of `other` are free. A match and a substitution
    need the two tokens within `tolerance` (beyond the other token's span)."""
    n, m = len(other), len(primary)
    if n == 0:
        return []
    if m == 0:
        return [None] * n
    pt = [p.t for p in primary]
    # column range of row i (cells j = number of primary tokens consumed), monotonic and overlapping row to row
    ranges: list[tuple[int, int]] = [(0, m)]
    for i, o in enumerate(other):
        a = bisect_left(pt, o.lo - BAND_SECONDS)
        b = bisect_right(pt, o.hi + BAND_SECONDS)
        pa, pb = ranges[-1]
        a = min(max(a, pa), pb)
        b = max(b, a, pb if i == 0 else a)
        ranges.append((a, min(m, b)))
    big = 1 << 30
    prev = [0] * (m + 1)  # row 0: leading primary tokens are free
    prev_a, prev_b = 0, m
    moves: list[bytearray] = [bytearray()]  # per row, the move into each cell of its range
    for i in range(1, n + 1):
        o = other[i - 1]
        a, b = ranges[i]
        cur = [big] * (b - a + 1)
        mv = bytearray(b - a + 1)
        for j in range(a, b + 1):
            best, move = big, 0
            if prev_a <= j <= prev_b:  # O token without a primary partner
                best, move = prev[j - prev_a] + 1, 2
            if j > a:  # primary token without a partner
                c = cur[j - 1 - a] + 1
                if c < best:
                    best, move = c, 3
            if j >= 1 and prev_a <= j - 1 <= prev_b:
                p = primary[j - 1]
                near = o.lo - tolerance <= p.t <= o.hi + tolerance
                if near:
                    d = prev[j - 1 - prev_a] + (0 if p.text == o.text else 1)
                    if d < best or (d == best and p.text == o.text):
                        best, move = d, 0 if p.text == o.text else 1
            cur[j - a] = best
            mv[j - a] = move
        moves.append(mv)
        prev, prev_a, prev_b = cur, a, b
    j = prev_a + min(range(len(prev)), key=lambda k: (prev[k], k))  # trailing primary tokens are free
    partner: list[int | None] = [None] * n
    i = n
    while i > 0:
        a, _ = ranges[i]
        move = moves[i][j - a]
        if move == 0:
            partner[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif move == 1:
            i, j = i - 1, j - 1
        elif move == 2:
            i -= 1
        else:
            j -= 1
    return partner


# ---- gaps -----------------------------------------------------------------------------------------------------


@dataclass
class RawGap:
    """A run of words of one source where the primary has no words."""

    source: str
    first: int  # word indices in the source, inclusive
    last: int
    text: str
    start: float  # in primary time
    end: float
    after: int  # index of the primary word before the gap (-1: before the first)
    silence: tuple[float, float]  # the primary's silence the gap sits in
    times: list[tuple[float, float]] = field(default_factory=list)  # per word, in primary time (word-level sources)


class _Primary:
    """The primary words with their start times, for searches by time."""

    def __init__(self, words: Sequence[Word]) -> None:
        self.words = words
        self.starts = [w.start for w in words]
        self.longest = max((w.end - w.start for w in words), default=0.0)

    def bounds(self, t: float) -> tuple[int, float, float]:
        """The primary word before time t (index, -1 if none) and the silence around t: end of that word to start
        of the next one."""
        k = bisect_right(self.starts, t) - 1
        lo = self.words[k].end if k >= 0 else 0.0
        hi = self.words[k + 1].start if k + 1 < len(self.words) else float("inf")
        return k, lo, hi

    def covered(self, a: float, b: float) -> bool:
        """Does a primary word (with COVER_TOLERANCE) overlap the moment [a, b]?"""
        lo = bisect_left(self.starts, a - COVER_TOLERANCE - self.longest)
        hi = bisect_right(self.starts, b + COVER_TOLERANCE)
        return any(w.end + COVER_TOLERANCE >= a for w in self.words[lo:hi])


def find_gaps(primary: Sequence[Word], source: Source) -> list[RawGap]:
    """Runs of words of `source` that the primary lacks at that time (see the module docstring)."""
    prim = _Primary(primary)
    ptoks = _tokens(primary, cue_level=False)
    otoks = _tokens(source.words, source.cue_level)
    partner = align(otoks, ptoks, CUE_TOLERANCE if source.cue_level else TIME_TOLERANCE)
    anchors = [(i, p) for i, p in enumerate(partner) if p is not None]
    anchor_index = [a for a, _ in anchors]
    gaps: list[RawGap] = []
    i = 0
    while i < len(otoks):
        if partner[i] is not None:
            i += 1
            continue
        j = i
        while j + 1 < len(otoks) and partner[j + 1] is None:
            j += 1
        gaps += [g for g in _run_gaps(prim, source, otoks, ptoks, anchors, anchor_index, i, j)
                 if not _repeats_neighbours(g, ptoks) and not (source.cue_level and _inside_cue(g, otoks[i:j + 1], ptoks))]
        i = j + 1
    return gaps


def _inside_cue(g: RawGap, run: list[_Tok], ptoks: list[_Tok]) -> bool:
    """Captions: does the primary have every word of the gap somewhere near the cue? Then the cue's coarse timing put
    a word the primary has at the wrong place: not a gap."""
    lo, hi = run[0].lo - CUE_TOLERANCE, run[-1].hi + CUE_TOLERANCE
    near = {t.text for t in ptoks if lo <= t.t <= hi}
    toks = normalise(g.text)
    return bool(toks) and all(t in near for t in toks)


def _repeats_neighbours(g: RawGap, ptoks: list[_Tok]) -> bool:
    """Is the gap only the primary words right before and after it again ("lanterns. [lanterns.]")? Then the
    alignment missed that the primary has them, typically at a caption cue's edge: not a gap."""
    return repeats_adjacent(normalise(g.text), g.after, ptoks, lambda a, b: a == b)


def repeats_adjacent(toks: list[str], after: int, ptoks: Sequence[_Tok], same: Callable[[str, str], bool]) -> bool:
    """Are the tokens `toks` of a gap after primary word `after` only the primary tokens right before the gap (its
    last ones) followed by those right after it (its first ones), compared with `same`?"""
    n = len(toks)
    if n == 0:
        return False
    k = bisect_right([t.word for t in ptoks], after)  # first primary token after the gap
    before = [t.text for t in ptoks[max(0, k - n) : k]]
    nxt = [t.text for t in ptoks[k : k + n]]
    for split in range(n + 1):
        if len(before) < split or len(nxt) < n - split:
            continue
        pairs = list(zip(toks[:split], before[len(before) - split :])) + list(zip(toks[split:], nxt[: n - split]))
        if all(same(a, b) for a, b in pairs):
            return True
    return False


def repeats_nearby(toks: list[str], after: int, primary: Sequence[Word]) -> bool:
    """Is every token of a gap after primary word `after` the same (same_spelling) as a token of the ADJACENT_WORDS
    primary words before or after the gap? Then it is those words heard again in the pause next to them."""
    near = [t for w in primary[max(0, after + 1 - ADJACENT_WORDS) : after + 1 + ADJACENT_WORDS] for t in normalise(w.text)]
    return bool(toks) and all(any(same_spelling(t, n) for n in near) for t in toks)


def edit_distance_at_most_one(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i + (len(a) == len(b)) :] == b[i + 1 :]


def same_spelling(a: str, b: str) -> bool:
    """Two normalised tokens that are the same word for `repeats-adjacent`: equal, or of 4+ letters and at most one
    letter apart ("Tandem" and "Tandom")."""
    return a == b or (min(len(a), len(b)) >= 4 and edit_distance_at_most_one(a, b))


def is_fragment(text: str) -> bool:
    """Only single letters or sounds that are no word: every word gives no comparison token (filler sounds,
    punctuation) or only tokens of one letter, without a vowel ("shh", "psst") or of one repeated letter ("ssss").
    A word written in capitals ("CRM") is an abbreviation, not a fragment. Real one-word answers ("Yes.", "No.",
    "Okay.") are not fragments."""
    for word in text.split():
        letters = [ch for ch in word if ch.isalpha()]
        if len(letters) >= 2 and all(ch.isupper() for ch in letters):
            return False
        for t in normalise(word):
            if not (len(t) == 1 or not (set(t) & _VOWELS) or len(set(t)) == 1):
                return False
    return True


def _run_gaps(prim: _Primary, source: Source, otoks: list[_Tok], ptoks: list[_Tok], anchors: list[tuple[int, int]],
              anchor_index: list[int], i: int, j: int) -> list[RawGap]:
    """Gaps inside the unanchored run of tokens i..j of the other source."""
    primary = prim.words
    k = bisect_left(anchor_index, i)
    left = anchors[k - 1] if k > 0 else None
    right = anchors[k] if k < len(anchors) else None
    a_lo = ptoks[left[1]].word if left else -1  # the primary words of the anchors around the run
    a_hi = ptoks[right[1]].word if right else len(primary)
    words = sorted({otoks[q].word for q in range(i, j + 1)})
    if source.cue_level:
        shift = 0.0
        t_lo, t_hi = otoks[i].lo, otoks[j].hi
    else:  # shift into primary time by the local offset of the anchors around the run
        offsets = [ptoks[a[1]].t - otoks[a[0]].t for a in (left, right) if a is not None]
        shift = sum(offsets) / len(offsets) if offsets else 0.0
        last = source.words[words[-1]]
        t_lo, t_hi = source.words[words[0]].start + shift, max(last.start, last.end) + shift
    # the primary words around the run's time (with a margin), between the anchors
    margin = CUE_MARGIN if source.cue_level else WORD_MARGIN
    p_lo = min(max(a_lo, bisect_right(prim.starts, t_lo - margin) - 1), a_hi - 1)
    p_hi = max(min(a_hi, bisect_left(prim.starts, t_hi + margin)), a_lo + 1)
    p_lo = min(p_lo, p_hi - 1)
    if p_hi - p_lo <= 1:  # the primary has no words there: the whole run is one gap
        g = _gap(prim, source, words[0], words[-1], p_lo, p_hi, t_lo, t_hi, shift)
        return [g] if g is not None else []
    if source.cue_level:
        return []  # the primary has words inside the cue: with cue-level times nothing can be placed
    # word-level: the run's words that no primary word covers, in pieces between primary words
    out: list[RawGap] = []
    piece: list[int] = []

    def flush() -> None:
        if piece:
            first, last = source.words[piece[0]], source.words[piece[-1]]
            lo_t, hi_t = first.start + shift, max(last.start, last.end) + shift
            q = prim.bounds(lo_t)[0]
            g = _gap(prim, source, piece[0], piece[-1], q, q + 1, lo_t, hi_t, shift)
            if g is not None:
                out.append(g)
            piece.clear()

    for w in words:
        sw = source.words[w]
        if prim.covered(sw.start + shift, sw.end + shift):
            flush()
        else:
            if piece and prim.bounds(source.words[piece[-1]].start + shift)[0] != prim.bounds(sw.start + shift)[0]:
                flush()  # a primary word lies between them
            piece.append(w)
    flush()
    return out


def _gap(prim: _Primary, source: Source, first: int, last: int, p_lo: int, p_hi: int, t_lo: float, t_hi: float,
         shift: float) -> RawGap | None:
    """The source's words first..last as a gap in the primary's silence between words p_lo and p_hi, if that silence
    has room for them."""
    primary = prim.words
    lo = primary[p_lo].end if p_lo >= 0 else 0.0
    hi = primary[p_hi].start if p_hi < len(primary) else float("inf")
    n = last - first + 1
    need = CUE_SECONDS_PER_WORD * n if source.cue_level else GAP_SILENCE_SHARE * (t_hi - t_lo)
    if hi - lo < max(MIN_GAP_SILENCE, need):
        return None
    text = " ".join(source.words[w].text for w in range(first, last + 1))
    if source.cue_level:
        start, end = max(lo, min(t_lo, hi)), min(hi, max(t_hi, lo))
        if end - start < CUE_SECONDS_PER_WORD * n:  # the cue and the silence hardly overlap: use the silence
            start, end = lo, (hi if hi != float("inf") else lo + CUE_SECONDS_PER_WORD * n + 1.0)
        return RawGap(source.name, first, last, text, start, end, p_lo, (lo, hi))
    times = [(source.words[w].start + shift, source.words[w].end + shift) for w in range(first, last + 1)]
    return RawGap(source.name, first, last, text, t_lo, t_hi, p_lo, (lo, hi), times)


# ---- merging and filling ----------------------------------------------------------------------------------------


def _priority(name: str) -> int:
    return SOURCE_PRIORITY.index(name) if name in SOURCE_PRIORITY else len(SOURCE_PRIORITY)


def _content_words(text: str) -> int:
    return sum(len(normalise(t)) > 0 for t in text.split())


def same_phrase(a: str, b: str) -> bool:
    """Do two gap texts say the same thing: at least SAME_PHRASE_SHARE of the shorter one's tokens are in the other?"""
    ta, tb = normalise_text(a), normalise_text(b)
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    common = sum((Counter(short) & Counter(long_)).values())
    return common >= max(1, SAME_PHRASE_SHARE * len(short))


def normalise_text(text: str) -> list[str]:
    return [t for w in text.split() for t in normalise(w)]


def cluster(gaps: Sequence[RawGap]) -> list[list[RawGap]]:
    """Gaps in the same primary silence that overlap or lie within CLUSTER_SECONDS of each other are one gap, and so
    are gaps of different sources in the same silence that are the same phrase (same_phrase), however far apart the
    sources timed them."""
    out: list[list[RawGap]] = []
    for g in sorted(gaps, key=lambda g: (g.after, g.start, _priority(g.source))):
        for c in out:
            if c[0].after == g.after and any(
                    (g.start <= h.end + CLUSTER_SECONDS and h.start <= g.end + CLUSTER_SECONDS)
                    or (h.source != g.source and same_phrase(g.text, h.text)) for h in c):
                c.append(g)
                break
        else:
            out.append([g])
    return out


@dataclass
class Result:
    words: list[TranscriptWord]
    completeness: Completeness


def complete(primary: Sequence[Word], primary_name: str, sources: Sequence[Source],
             speech_share: Callable[[float, float], float] | None = None,
             progress: Callable[[int, int, str], None] = lambda done, total, note: None,
             unavailable: Sequence[str] = ()) -> Result:
    """The primary words with every accepted gap filled in, and the completeness figures.

    `speech_share(a, b)`: the share of [a, b] with speech energy (None: no audio, nothing is rejected for silence).
    `unavailable`: cross-check sources that were not available, with the reason, for the record."""
    raw: list[RawGap] = []
    for n, s in enumerate(sources):
        progress(n, len(sources), f"comparing with {s.name}")
        raw += find_gaps(primary, s)
    progress(len(sources), len(sources), "filling gaps")
    gaps: list[Gap] = []
    fills: list[tuple[int, list[TranscriptWord]]] = []
    ptoks = _tokens(primary, cue_level=False)
    stoks = {s.name: (_tokens(s.words, s.cue_level), s.cue_level) for s in sources}
    for c in cluster(raw):
        names = sorted({g.source for g in c}, key=_priority)
        chosen = max(c, key=lambda g: (_content_words(g.text), -_priority(g.source)))
        words = _fill_words(chosen, False)
        status, reason = "unconfirmed", None
        if len(names) >= 2:
            missing = _unagreed(words, chosen.source, stoks)
            if not missing:
                status = "confirmed"
            else:
                reason = (f"found by {' and '.join(names)}, but no second source has "
                          f"{', '.join(dict.fromkeys(missing))} within the time tolerance")
        if repeats_nearby(normalise_text(chosen.text), chosen.after, primary):
            status, reason = "rejected", (f"repeats-adjacent: only words of the {ADJACENT_WORDS} primary words on "
                                          "either side of it again")
        elif len(names) == 1 and is_fragment(chosen.text):
            status, reason = "rejected", f"fragment: only {names[0]} has it, and it is only single letters or no word"
        elif len(names) == 1 and names[0] in HALLUCINATION_PRONE and speech_share is not None:
            # where the words would go, with a margin, inside the primary's silence
            a = max(chosen.silence[0], words[0].start - ENERGY_MARGIN)
            b = min(chosen.silence[1], words[-1].end + ENERGY_MARGIN)
            share = speech_share(a, b) if b > a else 0.0
            if share < MIN_SPEECH_SHARE or share * (b - a) < MIN_SPEECH_SECONDS:
                status = "rejected"
                reason = (f"no-speech: only {names[0]} has it, and {a:.2f}-{b:.2f} s has no speech energy "
                          f"({share:.0%} of the audio above the pause threshold)")
        for w in words:
            w.confirmed = status == "confirmed"
        gaps.append(Gap(start=round(words[0].start, 3), end=round(words[-1].end, 3), text=chosen.text,
                        words=len(words), sources=names, chosen=chosen.source, status=status, reason=reason))
        if status != "rejected":
            fills.append((chosen.after, words))
    merged = _merge(primary, fills)
    filled = sum(1 for w in merged if w.filled)
    per_source: dict[str, int] = {}
    for g in gaps:
        if g.status != "rejected":
            per_source[g.chosen] = per_source.get(g.chosen, 0) + g.words
    completeness = Completeness(
        primary=primary_name, primary_words=len(primary), sources=[s.name for s in sources],
        unavailable=list(unavailable), filled_words=filled, filled_by_source=per_source,
        filled_share=round(filled / len(merged), 5) if merged else 0.0,
        confirmed=sum(g.status == "confirmed" for g in gaps), unconfirmed=sum(g.status == "unconfirmed" for g in gaps),
        rejected=sum(g.status == "rejected" for g in gaps),
        rejected_by_reason=dict(Counter((g.reason or "").split(":")[0] for g in gaps if g.status == "rejected")),
        gaps=gaps,
    )
    return Result(words=merged, completeness=completeness)


def _unagreed(words: Sequence[TranscriptWord], chosen: str, stoks: dict[str, tuple[list[_Tok], bool]]) -> list[str]:
    """The normalised tokens of the filled `words` that no source other than `chosen` has within the time tolerance
    of the fill (TIME_TOLERANCE; for a cue-level source, CUE_TOLERANCE beyond the cue's span)."""
    lo, hi = words[0].start, words[-1].end
    return [t for w in words for t in normalise(w.text)
            if not any(_has_token(toks, t, lo, hi, cue) for name, (toks, cue) in stoks.items() if name != chosen)]


def _has_token(toks: list[_Tok], text: str, lo: float, hi: float, cue_level: bool) -> bool:
    tol = CUE_TOLERANCE if cue_level else TIME_TOLERANCE
    a = bisect_left(toks, lo - tol - BAND_SECONDS, key=lambda k: k.t)  # tokens are in time order; a cue is shorter
    b = bisect_right(toks, hi + tol + BAND_SECONDS, key=lambda k: k.t)  # than BAND_SECONDS
    return any(k.text == text and k.lo - tol <= hi and lo <= k.hi + tol for k in toks[a:b])


def _fill_words(g: RawGap, confirmed: bool) -> list[TranscriptWord]:
    """The gap's words with times inside the primary's silence, in order."""
    texts = g.text.split()
    lo, hi = g.silence
    if g.times and len(g.times) == len(texts):
        times = list(g.times)
    else:
        step = (g.end - g.start) / len(texts)
        times = [(g.start + step * q, g.start + step * (q + 1)) for q in range(len(texts))]
    out: list[TranscriptWord] = []
    last = lo
    for text, (a, b) in zip(texts, times):
        a = min(max(a, last, lo), hi)
        b = min(max(b, a), hi)
        out.append(TranscriptWord(text=text, start=round(a, 3), end=round(b, 3), source=g.source, filled=True,
                                  confirmed=confirmed))
        last = a
    return out


def _merge(primary: Sequence[Word], fills: list[tuple[int, list[TranscriptWord]]]) -> list[TranscriptWord]:
    """Primary words (unchanged) with each fill after the primary word it follows; fills at one place in time order."""
    after: dict[int, list[TranscriptWord]] = {}
    for k, words in sorted(fills, key=lambda f: (f[0], f[1][0].start)):
        after.setdefault(k, []).extend(words)
    for group in after.values():
        group.sort(key=lambda w: w.start)  # stable: the words of one fill keep their order
    out: list[TranscriptWord] = list(after.get(-1, []))
    for k, w in enumerate(primary):
        out.append(TranscriptWord(text=w.text, start=w.start, end=w.end, conf=w.conf))
        out += after.get(k, [])
    for a, b in zip(out, out[1:]):
        if b.start < a.start:
            raise ValueError(f"filled words out of order at {b.start:.3f} s")  # _fill_words keeps them inside the silence
    return out


def gate(c: Completeness, max_share: float, where: str = "work/transcript.json") -> str | None:
    """None when the filled share is within `max_share`; else the error for --strict-completeness, which points to
    `where`, the file with the figures and the gaps."""
    if c.filled_share <= max_share:
        return None
    return (f"{c.filled_words} of the transcript's words ({c.filled_share:.1%}) had to be filled in from other sources, "
            f"more than the {max_share:.1%} allowed: the primary speech recognition missed too much of the audio. "
            f"Check the audio and the gaps in {where}, or run without --strict-completeness to keep the result with a "
            "warning")


def above_limit_warning(c: Completeness, max_share: float) -> str | None:
    """The warning for a transcript whose filled share is above `max_share` (kept: filling is the remedy), else None."""
    if c.filled_share <= max_share:
        return None
    total = c.primary_words + c.filled_words
    return (f"{c.filled_words} of the transcript's {total} words ({c.filled_share:.1%}) were filled in from other "
            f"sources, more than the usual {max_share:.1%}: the primary speech recognition missed part of the speech. "
            "The filled-in words are marked [+ ... +] and [? ... ?]; check them against the recording")
