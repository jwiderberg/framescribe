"""Backend `parakeet`: parakeet-mlx in-process on chunks that are cut in pauses, decoded twice on two cut grids.

Unchunked decoding needs ~15 GB for 10 minutes of audio, and parakeet-mlx's own overlapping-chunk merge produced
duplicated and garbled words at the seams, so it is not used: the audio is cut here and each chunk is decoded alone.
Every word belongs to the chunk its start falls in, and cuts lie in pauses.

Measured on a 1 h 53 min recording against three independent transcripts (docs/MEASUREMENTS.md): ~120 s
chunks made the model skip whole sentences that shorter windows heard. Chunks now aim at `chunk_seconds` (30 s), at
the centre of the longest pause near the target, and are never longer than `chunk_max_seconds` (40 s). Each chunk is
decoded together with LEFT_CONTEXT_SECONDS of audio before its cut and RIGHT_CONTEXT_SECONDS after its end, without
leading silence; the words found in that context belong to the neighbouring chunks and are dropped. Of the
configurations measured this one missed the fewest phrases. A word that both chunks around a cut decoded is kept once
(drop_seam_duplicates).

The context does not stop the model from starting a sentence at a cut: measured on the same recording, the first word
after a cut was capitalised without a sentence stop before it at 24% of the cuts, against 3% for the other pass's
words at the same instants. So the casing of the first word after each cut is taken from the other pass when that
pass heard the same word (normalised) at that time (arbitrate_cut_casing); without such a word it is left as it is.
Only the casing changes, never the word.

A chunk is decoded again when it comes back bare (many words, no punctuation) or empty although its audio has speech
energy; of all decodes of a chunk the one with the most words is kept, a punctuated one before a bare one with as
many words (best_attempt), so a retry never loses words. Each such outcome that is still bare or empty is a warning.

A second pass decodes the audio again on a cut grid that puts its cuts near the middle of the first pass's chunks
(still in pauses), with the model loaded once. Its words are not merged here: the `asr` stage uses them as an
independent cross-check of the first pass (see completeness.py).
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import doctor
from ..chunking import Levels, read_wav, write_wav
from ..errors import NoAudioError, StageError
from ..models import AsrArtifact, AsrPass, Cut, IngestArtifact, Word
from ..words import (
    attach_leading_punctuation,
    finalize_words,
    segment_sentences,
    words_from_tokens,
)
from .base import Context, Stage

VERSION = 5
NAME = "asr.parakeet"
LEFT_CONTEXT_SECONDS = 1.0  # audio before the cut that is decoded with each chunk, its words dropped
RIGHT_CONTEXT_SECONDS = 2.0  # audio after the chunk's end that is decoded with it, its words dropped
PAD_SECONDS = 0.0  # digital silence before the decoded audio (measured: it made no consistent difference)
# A chunk that came back bare is decoded again with other context until it is not: the loss of punctuation depends
# on the exact audio the model sees and is not monotonic in the context (measured on a 1 h 53 min recording: a
# chunk bare with 1 s of context before it came back punctuated with 0.5 s or 2 s, another one without any), so the
# retries vary the context before the cut, then add leading silence, then RETRY_CONTEXT_SECONDS on both sides.
RETRY_LEFT_SECONDS = (0.0, 2.0, 0.5)
RETRY_PAD_SECONDS = (0.5, 2.0)
RETRY_CONTEXT_SECONDS = 8.0
BARE_MIN_WORDS = 10  # a chunk with at least this many words ...
BARE_MAX_PUNCTUATED = 0.03  # ... of which at most this fraction carry . , ? or ! is bare
LONE_WORD_MARGIN_SECONDS = 0.25  # a lone word this close to a cut without a gap counts as lying across it
NO_PAUSE_SHIFT_SECONDS = 5.0  # without a pause, the cut moves at most this far (and the cut search) to the widest gap between words
SEAM_OVERRUN_SECONDS = 0.02  # a word of the chunk before a cut that ends this far past it ran over it ...
SEAM_MATCH_SECONDS = 0.35  # ... and is a duplicate when the next chunk has it starting this close
MIN_CHUNK_FRACTION = 1 / 3  # the widened cut search never makes a chunk shorter than this share of the target
# A chunk without words whose audio has this many seconds of 10 ms frames above the pause threshold has speech the
# model did not decode. Measured on a 3 min excerpt: the same samples decoded to 31 words in a fresh process and to
# none after another chunk had been decoded, so decoding the same window again does not help; the retry widens the
# window to the left, to the longest pause EMPTY_RETRY_MIN to EMPTY_RETRY_MAX seconds before the chunk (or to the
# right, for a chunk at the start of the audio).
EMPTY_SPEECH_SECONDS = 1.0
EMPTY_RETRY_MIN, EMPTY_RETRY_MAX = 5.0, 15.0
CASE_MATCH_SECONDS = 0.5  # the other pass's word counts as the same word after a cut when it starts this close
_PUNCTUATED = re.compile(r"[.,?!]")


@dataclass(frozen=True)
class Decoding:
    """How each chunk is decoded: seconds of digital silence first, then context before and after the chunk."""

    pad: float = PAD_SECONDS
    left: float = LEFT_CONTEXT_SECONDS
    right: float = RIGHT_CONTEXT_SECONDS


def plan_cuts(levels: Levels, total: float, target: float, search: float, maximum: float | None = None) -> list[Cut]:
    """Cuts about `target` apart, each at the centre of the longest pause within `search` of the target, or
    within twice that. Where there is none, a `no_pause` cut at the target, to be moved to a word boundary when
    the chunk is decoded. With `maximum`, no chunk is longer than that (the searches stop there, and the widened
    one does not go below a third of the target); audio not longer than `maximum` (default target + search) stays
    whole."""
    limit = target + search if maximum is None else maximum
    cuts: list[Cut] = []
    last = 0.0
    while total - last > limit:
        nominal = last + target
        hi = nominal + search if maximum is None else min(nominal + search, last + maximum)
        if maximum is None:
            lo2, hi2 = max(last + search, nominal - 2 * search), min(total - search, nominal + 2 * search)
        else:
            lo2 = max(last + target * MIN_CHUNK_FRACTION, nominal - 2 * search)
            hi2 = min(total - search, nominal + 2 * search, last + maximum)
        pause = levels.longest_pause(nominal - search, hi, nominal) or levels.longest_pause(lo2, hi2, nominal)
        if pause is None:
            cuts.append(Cut(time=nominal, kind="no_pause", level_db=levels.at(nominal), pause_seconds=0.0))
        else:
            cuts.append(Cut(time=pause.centre, kind="pause", level_db=levels.at(pause.centre), pause_seconds=pause.seconds))
        last = cuts[-1].time
    return cuts


def plan_offset_cuts(levels: Levels, first: list[Cut], total: float, maximum: float) -> list[Cut]:
    """The second pass's grid: one cut near the middle of every chunk of the first pass, at the longest pause in
    its middle half (ties nearest the middle), or a `no_pause` cut near the middle where there is none. No chunk
    of this grid is longer than `maximum` either."""
    edges = [0.0, *(c.time for c in first), total]
    if len(edges) == 2:
        return []  # one chunk: there is nothing to offset against
    cuts: list[Cut] = []
    last = 0.0
    for a, b in zip(edges, edges[1:]):
        mid, quarter = (a + b) / 2, (b - a) / 4
        hi = min(mid + quarter, last + maximum)
        pause = levels.longest_pause(mid - quarter, hi, mid) if hi > mid - quarter else None
        if pause is None:
            near = min(mid, last + maximum - NO_PAUSE_SHIFT_SECONDS)
            cuts.append(Cut(time=near, kind="no_pause", level_db=levels.at(near), pause_seconds=0.0))
        else:
            cuts.append(Cut(time=pause.centre, kind="pause", level_db=levels.at(pause.centre), pause_seconds=pause.seconds))
        last = cuts[-1].time
    while total - last > maximum:
        # the tail after the last cut is too long for one chunk: more cuts, moved to a word boundary when decoded.
        # Never a failure: the second pass only cross-checks the first
        near = last + max(maximum - NO_PAUSE_SHIFT_SECONDS, maximum / 2)
        cuts.append(Cut(time=near, kind="no_pause", level_db=levels.at(near), pause_seconds=0.0))
        last = near
    return cuts


def is_bare(words: list[Word]) -> bool:
    """Many words but (almost) no punctuation: the model lost its sentence structure on this chunk."""
    punctuated = sum(1 for w in words if _PUNCTUATED.search(w.text))
    return len(words) >= BARE_MIN_WORDS and punctuated / len(words) <= BARE_MAX_PUNCTUATED


def gaps_near(words: list[Word], near: float, within: float) -> list[tuple[float, float]]:
    """(seconds between two neighbouring words, the middle of that gap) for every gap within `within` s of `near`."""
    return [(b.start - a.end, (a.end + b.start) / 2) for a, b in zip(words, words[1:])
            if abs((a.end + b.start) / 2 - near) <= within]


def word_boundary_cut(words: list[Word], near: float, within: float, levels: Levels) -> Cut:
    """A `no_pause` cut in the middle of the widest gap between two words within `within` seconds of `near`.
    Where the decode has no two words that close (music, noise, a language the model does not write down: sound
    without a pause and without words), the cut stays at `near`, or moves to the end of the one word that lies
    across it or within LONE_WORD_MARGIN_SECONDS of it. Always to its end, never to its start: the word is then kept from this decode. A cut at its start
    would leave it to the next decode, which times it on another frame grid and can put it just before the cut,
    where it counts as context and is dropped. It never fails: audio without words there loses nothing to a cut."""
    gaps = gaps_near(words, near, within)
    if not gaps:
        # also a word that starts just after `near`: the next decode can time it just before, see above
        across = [w for w in words if w.start - LONE_WORD_MARGIN_SECONDS < near < w.end + LONE_WORD_MARGIN_SECONDS]
        t = max(w.end for w in across) if across else near
        return Cut(time=t, kind="no_pause", level_db=levels.at(t), pause_seconds=0.0)
    gap, t = max(gaps, key=lambda g: (g[0], -abs(g[1] - near)))
    return Cut(time=t, kind="no_pause", level_db=levels.at(t), pause_seconds=max(0.0, gap))


def retry_decodings(decoding: Decoding) -> list[Decoding]:
    """The decodings to try, in order, for a chunk that came back bare."""
    tries = [Decoding(0.0, left, decoding.right) for left in RETRY_LEFT_SECONDS]
    tries += [Decoding(pad, 0.0, decoding.right) for pad in RETRY_PAD_SECONDS]
    tries.append(Decoding(0.0, RETRY_CONTEXT_SECONDS, RETRY_CONTEXT_SECONDS))
    return [d for i, d in enumerate(tries) if d != decoding and d not in tries[:i]]


def best_attempt(attempts: list[list[Word]]) -> int:
    """The index of the decode of a chunk to keep: the one with the most words; among those with as many words, a
    punctuated one (not bare) before a bare one; then the earliest. So a retry never replaces a decode that has more
    words, and a punctuated retry wins only when it lost no word."""
    return max(range(len(attempts)), key=lambda i: (len(attempts[i]), not is_bare(attempts[i]), -i))


def empty_retry_decoding(decoding: Decoding, levels: Levels, a: float, b: float, total: float) -> Decoding:
    """The one other window for a chunk [a, b) that came back without words: its left context reaches back to the
    longest pause EMPTY_RETRY_MIN to EMPTY_RETRY_MAX seconds before the chunk (EMPTY_RETRY_MAX without one); a chunk
    too close to the start of the audio gets up to EMPTY_RETRY_MAX more context after its end instead, and leading
    silence, so that the decoded audio differs even when the chunk also ends the audio."""
    if a < EMPTY_RETRY_MIN:
        return Decoding(RETRY_PAD_SECONDS[0], decoding.left, decoding.right + min(EMPTY_RETRY_MAX, max(0.0, total - b)))
    lo, hi = max(0.0, a - EMPTY_RETRY_MAX), a - EMPTY_RETRY_MIN
    pause = levels.longest_pause(lo, hi, (lo + hi) / 2)
    return Decoding(0.0, a - (pause.centre if pause is not None else lo), decoding.right)


def _letters_casing(word: str, model: str) -> str | None:
    """`word` with the upper and lower case of `model`'s letters, if both have the same letters ignoring case; else
    None. Nothing but the case of letters changes."""
    mine = [i for i, ch in enumerate(word) if ch.isalpha()]
    theirs = [ch for ch in model if ch.isalpha()]
    if len(mine) != len(theirs) or any(word[i].lower() != ch.lower() for i, ch in zip(mine, theirs)):
        return None
    out = list(word)
    for i, ch in zip(mine, theirs):
        out[i] = ch
    return "".join(out)


def arbitrate_cut_casing(words: list[Word], cuts: list[Cut], other: list[Word]) -> int:
    """At every cut, give the first word after it the casing that the other pass (which has no cut there) gave the
    same word at that time: same normalised tokens, starting within CASE_MATCH_SECONDS. Without such a word, the word
    is left as it is. Changes casing only, in place; returns the number of words changed."""
    from bisect import bisect_left

    from ..completeness import normalise

    starts = [w.start for w in words]
    other_starts = [w.start for w in other]
    changed = 0
    for cut in cuts:
        k = bisect_left(starts, cut.time)
        if k >= len(words):
            continue
        w = words[k]
        lo = bisect_left(other_starts, w.start - CASE_MATCH_SECONDS)
        near = [o for o in other[lo:] if o.start <= w.start + CASE_MATCH_SECONDS]
        matches = sorted((o for o in near if normalise(o.text) == normalise(w.text)), key=lambda o: abs(o.start - w.start))
        if not matches or not normalise(w.text):
            continue
        cased = _letters_casing(w.text, matches[0].text)
        if cased is not None and cased != w.text:
            w.text = cased
            changed += 1
    return changed


def drop_seam_duplicates(words: list[Word], raw: list[Word], cut: float) -> None:
    """With context after its end, the chunk before `cut` can decode the first word after the cut and time it as
    starting just before it, so the word comes out twice. Drop such a word from the end of `words` (the chunk
    before) when the chunk after the cut, whose decode `raw` is, has the same word at about the same time, at or
    after the cut. Words are only dropped when the other chunk keeps them."""
    from ..completeness import normalise  # the same comparison as the cross-check

    after = [q for q in raw if q.start >= cut and q.start <= cut + 2 * SEAM_MATCH_SECONDS]
    while words and words[-1].end > cut + SEAM_OVERRUN_SECONDS:
        w = words[-1]
        if not any(normalise(q.text) == normalise(w.text) and abs(q.start - w.start) <= SEAM_MATCH_SECONDS for q in after):
            break
        words.pop()


@dataclass
class PassResult:
    words: list[Word]
    cuts: list[Cut]
    warnings: list[str]
    seconds: float


def decode_pass(model, samples: np.ndarray, rate: int, levels: Levels, planned: list[Cut], decoding: Decoding,
                work_dir: Path, search: float, progress: Callable[[float, str], None] = lambda done, note: None,
                warn: Callable[[str], None] = lambda message: None, label: str = "") -> PassResult:
    """Decode the audio chunk by chunk along the planned cuts. Words come back in time order, unfinalised."""
    t0 = time.perf_counter()
    audio_seconds = len(samples) / rate
    words: list[Word] = []
    cuts: list[Cut] = []
    warnings: list[str] = []

    def warning(text: str) -> None:
        warnings.append(text)
        warn(text)

    last_raw: list[Word] = []  # every word of the latest decode, its context's words too

    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        chunk_wav = Path(tmp) / "chunk.wav"

        def decode(a: float, b: float, end: float, how: Decoding = decoding) -> list[Word]:
            """Words starting in [a, b), decoded from the audio in [a - left, end + right) after `pad` seconds of
            silence. The context's own words start before `a` or at `b` and later (the cuts lie in pauses) and
            are dropped."""
            first = max(0, round((a - how.left) * rate))
            last = min(len(samples), round((end + how.right) * rate))
            audio = samples[first:last]
            pad = how.pad
            write_wav(chunk_wav, np.concatenate([np.zeros(round(pad * rate), np.int16), audio]), rate)
            result = model.transcribe(chunk_wav)
            offset = first / rate - pad
            raw = [Word(text=w.text, start=max(0.0, w.start + offset), end=max(0.0, w.end + offset), conf=w.conf)
                   for w in words_from_tokens(s.tokens for s in result.sentences)]
            last_raw[:] = raw
            return [w for w in raw if a <= w.start < b]

        a = 0.0
        for i, planned_cut in enumerate([*planned, None], start=1):
            progress(a, f"chunk {i}/{len(planned) + 1}")
            if planned_cut is not None and planned_cut.kind == "no_pause":
                # Decode past the target and cut in the widest gap between the words of that decode; the words
                # before the cut are kept from this same decode, so the boundary is the one it found.
                shift = min(NO_PAUSE_SHIFT_SECONDS, search)
                nxt = planned[i].time if i < len(planned) else audio_seconds
                within = max(0.5, min(shift, planned_cut.time - a - 0.5, nxt - planned_cut.time - 0.5))
                end = min(audio_seconds, planned_cut.time + 2 * shift + 2.0)
                span = decode(a, end, end)
                cut = word_boundary_cut(span, planned_cut.time, within, levels)
                chunk = [w for w in span if w.start < cut.time]
                where = (f"between two words ({cut.pause_seconds:.2f} s apart, {cut.level_db:.1f} dBFS)"
                         if gaps_near(span, planned_cut.time, within) else f"where the decode has no two words to cut between "
                         f"({cut.level_db:.1f} dBFS: sound without speech the model writes down)")
                warning(f"{label}no pause near {planned_cut.time:.2f} s: cut at {cut.time:.2f} s {where}")
            else:
                cut = planned_cut
                end = audio_seconds if cut is None else cut.time
                chunk = decode(a, end, end)
            b = audio_seconds if cut is None else cut.time
            # every decode of this chunk with all the words it found (context included), the best one is kept
            attempts: list[tuple[list[Word], list[Word]]] = [(chunk, list(last_raw))]
            retries = retry_decodings(decoding)
            best = 0
            while is_bare(attempts[best][0]) and retries:
                attempts.append((decode(a, b, b, retries.pop(0)), list(last_raw)))
                best = best_attempt([x for x, _ in attempts])
            speech = levels.speech_share(a, b) * (b - a)
            if not attempts[best][0] and speech >= EMPTY_SPEECH_SECONDS:
                how = empty_retry_decoding(decoding, levels, a, b, audio_seconds)
                attempts.append((decode(a, b, b, how), list(last_raw)))
                best = best_attempt([x for x, _ in attempts])
                if not attempts[best][0]:
                    warning(f"{label}chunk {a:.2f}-{b:.2f} s: no words, although {speech:.1f} s of it is above the "
                            f"pause threshold, also after a retry with {how.left:.1f} s of context before it and "
                            f"{how.right:.1f} s after it")
            chunk, raw = attempts[best]
            if is_bare(chunk):
                punctuated = [len(x) for x, _ in attempts if x and not is_bare(x)]
                fewer = f"; a punctuated decode had only {max(punctuated)} words" if punctuated else ""
                warning(f"{label}chunk {a:.2f}-{b:.2f} s: {len(chunk)} words without punctuation kept (the decode with "
                        f"the most words of {len(attempts)}, with other context and leading silence{fewer}); "
                        "sentences there are split at pauses only")
            if cuts:
                drop_seam_duplicates(words, raw, cuts[-1].time)
            words.extend(chunk)
            if cut is not None:
                cuts.append(cut)
                a = cut.time
    return PassResult(words=words, cuts=cuts, warnings=warnings, seconds=time.perf_counter() - t0)


def run(ctx: Context) -> AsrArtifact:
    cfg = ctx.config
    if cfg.chunk_seconds <= 0 or cfg.cut_search_seconds < 0 or cfg.cut_search_seconds >= cfg.chunk_seconds:
        raise StageError(f"need 0 <= cut search ({cfg.cut_search_seconds}) < chunk ({cfg.chunk_seconds}) seconds")
    if cfg.chunk_max_seconds < cfg.chunk_seconds + NO_PAUSE_SHIFT_SECONDS:
        raise StageError(f"the longest chunk ({cfg.chunk_max_seconds} s) must be at least the target "
                         f"({cfg.chunk_seconds} s) plus {NO_PAUSE_SHIFT_SECONDS:g} s")
    if cfg.mlx_cache_limit_mb < 0:
        raise StageError(f"need 0 <= MLX cache limit ({cfg.mlx_cache_limit_mb}) MB")
    ingest = ctx.get("ingest", IngestArtifact)
    if ingest.audio_wav is None:
        raise NoAudioError(f"{ctx.source} has no audio stream, so there is nothing to transcribe; use --transcript")
    # The only code that may download the model is `framescribe setup`: whoever calls this stage (the CLI, or a program
    # using the pipeline as a library) gets Hugging Face's client in offline mode and a StageError, not a download.
    if (problem := doctor.uncached_model_problem(cfg.parakeet_model)) is not None:
        raise StageError(problem)
    samples, rate = read_wav(ctx.run_dir / ingest.audio_wav)
    audio_seconds = len(samples) / rate
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")  # keep Hugging Face's token warning out of the status display
    from parakeet_mlx import (
        from_pretrained,  # imported here: pulls in MLX, which other stages do not need
    )

    t0 = time.perf_counter()
    memory: dict[str, float | int] = {}
    with mlx_memory_scope(cfg.mlx_cache_limit_mb, ctx.log, memory):
        total = 2 * audio_seconds  # two passes over the audio
        ctx.progress(0, total, "audio_s", "loading model")
        # the pinned snapshot's folder: given the repository name, parakeet-mlx would read the branch main
        model = from_pretrained(doctor.model_load_path(cfg.parakeet_model))
        levels = Levels(samples, rate)
        decoding = Decoding()
        planned = plan_cuts(levels, audio_seconds, cfg.chunk_seconds, cfg.cut_search_seconds, cfg.chunk_max_seconds)
        first = decode_pass(model, samples, rate, levels, planned, decoding, ctx.work_dir, cfg.cut_search_seconds,
                            progress=lambda done, note: ctx.progress(done, total, "audio_s", f"pass 1, {note}"),
                            warn=ctx.log)
        second_plan = plan_offset_cuts(levels, first.cuts, audio_seconds, cfg.chunk_max_seconds)
        second = decode_pass(model, samples, rate, levels, second_plan, decoding, ctx.work_dir, cfg.cut_search_seconds,
                             progress=lambda done, note: ctx.progress(audio_seconds + done, total, "audio_s", f"pass 2, {note}"),
                             warn=ctx.log, label="pass 2: ")
    ctx.metrics.update(memory)
    ctx.progress(total, total, "audio_s", "building words")
    words = finalize_words(attach_leading_punctuation(first.words), audio_seconds)
    words2 = finalize_words(attach_leading_punctuation(second.words), audio_seconds)
    # each pass is the arbiter of the other's casing at its cuts, both judged on the words as decoded
    decoded1, decoded2 = [w.model_copy() for w in words], [w.model_copy() for w in words2]
    recased = (arbitrate_cut_casing(words, first.cuts, decoded2), arbitrate_cut_casing(words2, second.cuts, decoded1))
    ctx.log(f"casing of the first word after a cut taken from the other pass: {recased[0]} words in pass 1, "
            f"{recased[1]} in pass 2")
    return AsrArtifact(
        backend="parakeet",
        model=cfg.parakeet_model,
        language=None,  # parakeet v3 detects the language and does not report it
        words=words,
        segments=segment_sentences(words),
        audio_seconds=audio_seconds,
        wall_seconds=time.perf_counter() - t0,
        cuts=first.cuts,
        warnings=first.warnings + second.warnings,
        second_pass=AsrPass(name="parakeet-pass2", words=words2, cuts=second.cuts),
        pass_seconds=[round(first.seconds, 3), round(second.seconds, 3)],
    )


@contextmanager
def mlx_memory_scope(cache_limit_mb: int, log: Callable[[str], None], stats: dict[str, float | int]) -> Iterator[None]:
    """Bound MLX's buffer cache while the stage decodes, and give the memory back and restore the limit that was in
    force before when it ends, so other code in the process is unaffected.

    MLX keeps every freed buffer for reuse, and its default limit for that cache is the whole memory limit (more
    than the RAM). Chunks differ in length, so their buffers rarely fit each other: measured on 10 minutes of audio,
    the cache grew by about 0.5 GB per decode to 26 GB while the memory in use stayed flat at 1.2 GB. With a limit,
    MLX frees cached buffers once the cache exceeds it. `stats` receives the stage's peak MLX memory (MB)."""
    import mlx.core as mx

    previous = mx.set_cache_limit(cache_limit_mb * 1024 * 1024)
    mx.reset_peak_memory()
    log(f"MLX cache limit {cache_limit_mb} MB (was {previous / 1024 / 1024:.0f} MB)")
    try:
        yield
    finally:
        peak = mx.get_peak_memory() / 1024 / 1024
        mx.clear_cache()
        mx.set_cache_limit(previous)
        stats["mlx_peak_mb"] = round(peak, 1)
        stats["mlx_cache_limit_mb"] = cache_limit_mb
        log(f"MLX peak memory {peak:.0f} MB")


STAGE = Stage(
    name=NAME,
    version=VERSION,
    filename="asr.parakeet.json",
    model=AsrArtifact,
    run=run,
    config_fields=("parakeet_model", "chunk_seconds", "cut_search_seconds", "chunk_max_seconds"),
    deps=("ingest",),
    summary=AsrArtifact.describe,
)
