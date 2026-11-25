"""Completeness cross-check: gaps that other sources show in the primary transcript, and filling them in, marked."""

import numpy as np
import pytest

from framescribe.chunking import write_wav
from framescribe.completeness import Source, above_limit_warning, caption_source, complete, find_gaps, gate, normalise
from framescribe.config import Config
from framescribe.models import (
    AsrArtifact,
    AsrPass,
    AudioFacts,
    Caption,
    Completeness,
    IngestArtifact,
    Layout,
    Rect,
    VideoFacts,
    Word,
)
from framescribe.stages import asr
from framescribe.stages.base import Context, StageError
from framescribe.words import segment_sentences

RATE = 16000


def words(text: str, start: float, step: float = 0.4) -> list[Word]:
    """Words spoken one every `step` seconds from `start`, each lasting most of that."""
    return [Word(text=t, start=round(start + i * step, 3), end=round(start + i * step + step * 0.8, 3))
            for i, t in enumerate(text.split())]


BEFORE = "So let us check the big departure board."
DROPPED = "Will the night express stop at Marlow?"
AFTER = "Good that matches the printed timetable."


def primary_without_phrase() -> list[Word]:
    # the phrase was said from 5.0 s to about 7.8 s; the primary has nothing there
    return words(BEFORE, 1.0) + words(AFTER, 8.5)


def full_source(shift: float = 0.0) -> list[Word]:
    return words(BEFORE, 1.0 + shift) + words(DROPPED, 5.0 + shift) + words(AFTER, 8.5 + shift)


def test_normalise_maps_engine_spellings_to_the_same_tokens():
    assert normalise("Gonna") == ["going", "to"]
    assert normalise("it's") == ["it", "is"] and normalise("don't") == ["do", "not"]
    assert normalise("10%") == ["ten", "percent"] and normalise("1,500") == ["one", "thousand", "five", "hundred"]
    assert normalise("Uh,") == [] and normalise("OK.") == ["okay"] and normalise("e-mail") == ["e", "mail"]


def test_a_dropped_phrase_is_found_and_filled_in_its_place():
    primary = primary_without_phrase()
    (gap,) = find_gaps(primary, Source("parakeet-pass2", full_source(shift=0.3)))  # another decode, timings a bit off
    assert gap.text == DROPPED and gap.after == len(BEFORE.split()) - 1
    r = complete(primary, "parakeet", [Source("parakeet-pass2", full_source(0.3)), Source("whisperkit", full_source(-0.2))])
    c = r.completeness
    assert (c.confirmed, c.unconfirmed, c.rejected, c.filled_words) == (1, 0, 0, len(DROPPED.split()))
    assert c.gaps[0].sources == ["parakeet-pass2", "whisperkit"] and c.gaps[0].status == "confirmed"
    assert " ".join(w.text for w in r.words) == f"{BEFORE} {DROPPED} {AFTER}"
    filled = [w for w in r.words if w.filled]
    assert {w.source for w in filled} == {"parakeet-pass2"} and all(w.confirmed for w in filled)
    # primary words are kept exactly as they were
    assert [(w.text, w.start, w.end) for w in r.words if not w.filled] == [(w.text, w.start, w.end) for w in primary]


def test_wording_differences_between_engines_are_not_gaps():
    primary = words("Right I'm gonna print the Fennel timetable and it's 10 percent cheaper, uh, okay.", 1.0)
    other = words("Right I am going to print the Fennell timetable and it is ten percent cheaper okay.", 1.1, step=0.36)
    assert find_gaps(primary, Source("whisperkit", other)) == []
    # a word one engine has inside fluent speech, where the primary leaves no room, is not a gap either
    assert find_gaps(words("print the ticket now", 1.0), Source("whisperkit", words("print out the ticket now", 1.0, 0.32))) == []


def test_a_single_source_whisperkit_gap_in_silence_is_rejected_and_not_filled():
    primary = primary_without_phrase()
    silent = lambda a, b: 0.0  # noqa: E731 - no speech energy anywhere in the gap
    r = complete(primary, "parakeet", [Source("whisperkit", full_source())], speech_share=silent)
    (g,) = r.completeness.gaps
    assert g.status == "rejected" and "no speech energy" in g.reason and g.sources == ["whisperkit"]
    assert r.completeness.filled_words == 0 and not any(w.filled for w in r.words)
    # with speech there, the same gap is filled, unconfirmed
    r = complete(primary, "parakeet", [Source("whisperkit", full_source())], speech_share=lambda a, b: 0.8)
    assert r.completeness.unconfirmed == 1 and all(w.confirmed is False for w in r.words if w.filled)
    # a gap that only the second parakeet pass has is not subject to the silence rule
    r = complete(primary, "parakeet", [Source("parakeet-pass2", full_source())], speech_share=silent)
    assert r.completeness.unconfirmed == 1 and r.completeness.rejected == 0


def test_caption_only_gap_with_cue_level_timing_is_filled_inside_the_cue():
    primary = primary_without_phrase()
    cues = [Caption(start=0.8, end=4.6, text=BEFORE), Caption(start=4.6, end=8.2, text=DROPPED),
            Caption(start=8.2, end=11.5, text=AFTER)]
    (gap,) = find_gaps(primary, caption_source(cues))
    assert gap.text == DROPPED
    r = complete(primary, "parakeet", [caption_source(cues)])
    filled = [w for w in r.words if w.filled]
    assert " ".join(w.text for w in filled) == DROPPED and {w.source for w in filled} == {"captions"}
    gap_lo, gap_hi = primary[len(BEFORE.split()) - 1].end, primary[len(BEFORE.split())].start
    assert all(gap_lo <= w.start <= w.end <= gap_hi for w in filled)
    # a cue whose words the primary has, worded differently, is no gap
    other = [Caption(start=0.5, end=5.0, text="So let's check the big departure board")]
    assert find_gaps(words(BEFORE, 1.0), caption_source(other)) == []


def test_word_order_stays_monotonic_after_filling_several_gaps():
    primary = words("one two three", 0.0) + words("seven eight", 6.0) + words("twelve", 12.0)
    other = words("one two three four five six", 0.0, 0.5) + words("seven eight nine ten eleven", 6.0, 0.5) + words("twelve", 12.0)
    early = words("zero", 0.0, 0.3)
    shifted = [Word(text=w.text, start=w.start - 0.1, end=w.end + 0.6) for w in other]  # overlapping, sloppy timings
    r = complete(primary[0:0] + primary, "parakeet",
                 [Source("parakeet-pass2", shifted), Source("whisperkit", other + early[0:0])])
    starts = [w.start for w in r.words]
    assert starts == sorted(starts)
    assert all(a.end <= b.start + 1e-9 or b.filled or a.filled for a, b in zip(r.words, r.words[1:]))
    assert [w.text for w in r.words] == ("one two three four five six seven eight nine ten eleven twelve").split()
    for seg in segment_sentences(r.words):
        assert seg.start <= seg.end


def test_gate_message_above_the_threshold():
    primary = primary_without_phrase()
    c = complete(primary, "parakeet", [Source("parakeet-pass2", full_source())]).completeness
    assert c.filled_share == pytest.approx(7 / (len(primary) + 7), abs=1e-4)
    assert gate(c, max_share=0.5) is None and above_limit_warning(c, max_share=0.5) is None
    message = gate(c, max_share=0.03)  # the error under --strict-completeness
    assert message is not None and "7 of the transcript's words" in message and "--strict-completeness" in message


def asr_context(tmp_path, cfg: Config, captions=(), primary=None, second=None, seconds=12.0):
    rng = np.random.default_rng(0)
    (tmp_path / "work").mkdir(exist_ok=True)
    write_wav(tmp_path / "work/audio.wav", rng.normal(0, 3000, int(seconds * RATE)).astype(np.int16), RATE)
    ingest = IngestArtifact(source="v.mp4", duration=seconds, video=VideoFacts(width=1, height=1, fps=1, codec="h264"),
                            audio=AudioFacts(codec="aac", sample_rate=RATE, channels=1), audio_wav="work/audio.wav",
                            embedded_captions=list(captions),
                            layout=Layout(frame_w=1, frame_h=1, content_rect=Rect(x=0, y=0, w=1, h=1), method="m", confidence=1))
    primary = primary_without_phrase() if primary is None else primary
    parakeet = AsrArtifact(backend="parakeet", model="m", language=None, words=primary, segments=segment_sentences(primary),
                           audio_seconds=seconds, wall_seconds=1.0,
                           second_pass=AsrPass(name="parakeet-pass2", words=full_source() if second is None else second, cuts=[]))
    logs = []
    ctx = Context(config=cfg, source=tmp_path / "v.mp4", run_dir=tmp_path, stage="asr",
                  artifacts={"ingest": ingest, "asr.parakeet": parakeet},
                  emit=lambda event, **f: logs.append(f.get("message")) if event == "log" else None)
    return ctx, logs


def test_asr_stage_warns_above_the_threshold_and_fails_only_when_strict(tmp_path):
    # default limit 3%: 7 filled of 21 words is far above it; the transcript is kept, with the warning
    ctx, logs = asr_context(tmp_path, Config())
    t = asr.run(ctx, ("asr.parakeet",))
    c = t.completeness
    assert c.above_limit and c.limit == 0.03
    assert c.warning == above_limit_warning(c, 0.03) and "7 of the transcript's 21 words (33.3%)" in c.warning
    assert c.warning in logs and "ABOVE THE USUAL 3.0%" in asr.describe(t)
    assert "[+ " not in t.segments[1].text and f"[? {DROPPED} ?]" in " ".join(s.text for s in t.segments)
    # the summary says the second pass was the only cross-check, rather than implying full coverage
    assert "second parakeet pass only" in asr.describe(t)
    assert t.completeness.unavailable == ["whisperkit: not part of this run", "captions: the video has no embedded captions"]
    # --strict-completeness: the old behaviour, an error and the refused transcript for diagnosis
    ctx, _ = asr_context(tmp_path, Config(strict_completeness=True))
    with pytest.raises(StageError, match=r"\(33\.3%\).*more than the 3\.0% allowed"):
        asr.run(ctx, ("asr.parakeet",))
    assert (tmp_path / "work" / asr.GATE_FAILED).is_file()
    # within the limit: no warning (and the diagnostics file of the refused run is gone)
    ctx, _ = asr_context(tmp_path, Config(max_filled_share=0.5, strict_completeness=True))
    c = asr.run(ctx, ("asr.parakeet",)).completeness
    assert c.warning is None and not c.above_limit and c.limit == 0.5
    assert not (tmp_path / "work" / asr.GATE_FAILED).exists()


def figures(primary_words: int, filled_words: int) -> Completeness:
    total = primary_words + filled_words
    return Completeness(primary="parakeet", primary_words=primary_words, sources=["parakeet-pass2"],
                        filled_words=filled_words, filled_by_source={"parakeet-pass2": filled_words},
                        filled_share=round(filled_words / total, 5), confirmed=0, unconfirmed=1, rejected=0)


def test_above_the_limit_the_warning_says_how_much_was_filled_in_plainly():
    for primary_words, filled, share in ((31, 11, "26.2%"), (1552, 128, "7.6%")):  # a short clip, a 25-minute video
        c = figures(primary_words, filled)
        warning = above_limit_warning(c, 0.03)
        assert warning.startswith(f"{filled} of the transcript's {primary_words + filled} words ({share}) were filled in")
        assert "more than the usual 3.0%" in warning and "check them against the recording" in warning
        assert gate(c, 0.03) is not None  # what --strict-completeness refuses
    # at the limit and under it nothing is said
    assert above_limit_warning(figures(97, 3), 0.03) is None and gate(figures(97, 3), 0.03) is None
    assert above_limit_warning(figures(1970, 30), 0.03) is None


SHORT_A = "Here we open the station list and pick the northern line for the early slot today."
SHORT_DROPPED = "Then the planner shows seven trains between Ashby and Corran Bay."
SHORT_B = "We take the second one because it stops at every village along the coast road."


def test_a_short_recording_with_a_large_filled_share_succeeds_with_the_warning(tmp_path):
    primary = words(SHORT_A, 1.0) + words(SHORT_B, 13.0)
    second = words(SHORT_A, 1.0) + words(SHORT_DROPPED, 8.0) + words(SHORT_B, 13.0)
    assert (len(primary), len(SHORT_DROPPED.split())) == (31, 11)
    ctx, logs = asr_context(tmp_path, Config(), primary=primary, second=second, seconds=20.0)
    c = asr.run(ctx, ("asr.parakeet",)).completeness  # the default config
    assert (c.filled_words, c.primary_words, c.filled_share) == (11, 31, pytest.approx(11 / 42, abs=1e-4))
    assert c.above_limit and c.warning.startswith("11 of the transcript's 42 words (26.2%) were filled in")
    assert c.warning in logs and not (tmp_path / "work" / asr.GATE_FAILED).exists()
    ctx, _ = asr_context(tmp_path, Config(strict_completeness=True), primary=primary, second=second, seconds=20.0)
    with pytest.raises(StageError, match=r"11 of the transcript's words \(26\.2%\)"):
        asr.run(ctx, ("asr.parakeet",))
    assert (tmp_path / "work" / asr.GATE_FAILED).is_file()


def test_a_long_recording_with_128_of_1680_words_filled_succeeds_with_the_warning(tmp_path):
    # 40 sentences of 42 words, one word every 0.4 s; the primary missed a 4-word phrase (3.2 s) in 32 of them
    vocab = ("route timetable platform carriage ticket station express coastal northern village harbour signal "
             "junction depot shuttle ferry").split()
    primary, second, t = [], [], 0.0
    for n in range(40):
        sentence = " ".join(vocab[(n + k) % len(vocab)] + str(n) for k in range(38 if n < 32 else 42))
        said = words(sentence, t)
        primary += said
        second += said
        t = said[-1].end + 0.2
        if n < 32:
            phrase = words(f"missed{n} words{n} were{n} here{n}", t + 0.5)
            second += phrase
            t = phrase[-1].end + 0.7
    assert (len(primary), len(second) - len(primary)) == (1552, 128)
    ctx, _ = asr_context(tmp_path, Config(), primary=primary, second=second, seconds=t + 1)
    c = asr.run(ctx, ("asr.parakeet",)).completeness
    assert (c.filled_words, c.primary_words) == (128, 1552) and c.above_limit
    assert c.warning.startswith("128 of the transcript's 1680 words (7.6%) were filled in")
    ctx, _ = asr_context(tmp_path, Config(strict_completeness=True), primary=primary, second=second, seconds=t + 1)
    with pytest.raises(StageError, match=r"128 of the transcript's words \(7\.6%\)"):
        asr.run(ctx, ("asr.parakeet",))
    assert (tmp_path / "work" / asr.GATE_FAILED).is_file()


def test_asr_stage_confirms_a_gap_that_captions_also_have(tmp_path):
    cues = [Caption(start=0.8, end=4.6, text=BEFORE), Caption(start=4.6, end=8.2, text=DROPPED),
            Caption(start=8.2, end=11.5, text=AFTER)]
    ctx, _ = asr_context(tmp_path, Config(max_filled_share=0.5), captions=cues)
    t = asr.run(ctx, ("asr.parakeet",))
    assert t.completeness.confirmed == 1 and t.completeness.sources == ["parakeet-pass2", "captions"]
    assert f"[+ {DROPPED} +]" in " ".join(s.text for s in t.segments)
    assert "second parakeet pass only" not in asr.describe(t)


def test_a_caption_cue_that_lags_and_repeats_a_neighbouring_word_is_no_gap():
    # captions come in 4 s buckets placed after the speech: the first word of a cue is often said seconds before it
    primary = words("The ferry is about to leave.", 1.0) + words("Tickets are sold on board.", 5.0)
    cues = [Caption(start=0.0, end=4.0, text="The ferry is about to"), Caption(start=6.5, end=10.5,
                                                                             text="leave. Tickets are sold on board.")]
    assert find_gaps(primary, caption_source(cues)) == []
    # the same for a word-timed source that heard a neighbouring word twice
    other = words("The ferry is about to leave. leave.", 1.0) + words("Tickets are sold on board.", 5.0)
    assert find_gaps(primary, Source("whisperkit", other)) == []


def test_ordinals_normalise_to_their_spelled_form():
    for n, spelled in ((1, "first"), (2, "second"), (3, "third"), (5, "fifth"), (8, "eighth"), (9, "ninth"),
                       (11, "eleventh"), (12, "twelfth"), (13, "thirteenth"), (20, "twentieth")):
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n, "th")
        assert normalise(f"{n}{suffix}") == [spelled] == normalise(spelled.capitalize() + ",")
    assert normalise("21st") == ["twenty", "first"] == normalise("twenty-first") and normalise("40th") == ["fortieth"]
    # the ordinal on one side and the spelled one on the other is no gap
    assert find_gaps(words("the 3rd platform is closed", 1.0), Source("whisperkit", words("the third platform is closed", 1.0))) == []


def test_a_word_heard_again_in_the_pause_next_to_itself_is_rejected_as_repeats_adjacent():
    primary = words("we wait at Oakmere", 1.0) + words("and it leaves from the east pier", 4.0)
    for heard in ("Oakmare", "Oakmire"):  # another spelling of the same name (the very word again is no gap at all)
        other = words("we wait at", 1.0) + words(heard, 2.9) + words("and it leaves from the east pier", 4.0)
        r = complete(primary, "parakeet", [Source("whisperkit", other)], speech_share=lambda a, b: 0.9)
        (g,) = r.completeness.gaps
        assert g.status == "rejected" and g.reason.startswith("repeats-adjacent"), heard
        assert r.completeness.rejected_by_reason == {"repeats-adjacent": 1} and r.completeness.filled_words == 0
        assert "repeats-adjacent" in r.completeness.summary()
    # a word of the three primary words on either side, heard again in the pause: rejected as well
    primary2 = words("then we board the night ferry okay", 1.0) + words("and it leaves from the east pier", 6.0)
    other = primary2[:7] + words("Ferry.", 4.5) + primary2[7:]
    r = complete(primary2, "parakeet", [Source("parakeet-pass2", other)])
    assert r.completeness.rejected_by_reason == {"repeats-adjacent": 1}
    # ... but not a word that only an earlier part of the sentence has
    other = primary2[:7] + words("board", 4.5) + primary2[7:]
    assert complete(primary2, "parakeet", [Source("parakeet-pass2", other)]).completeness.unconfirmed == 1
    # a word of 3 letters one letter off, or a different word, is no repeat: filled
    other = words("we wait at", 1.0) + words("Oakmerean", 2.9) + words("and it leaves from the east pier", 4.0)
    r = complete(primary, "parakeet", [Source("whisperkit", other)], speech_share=lambda a, b: 0.9)
    assert r.completeness.unconfirmed == 1 and r.completeness.rejected == 0


def test_a_single_source_gap_of_letters_or_hissing_is_rejected_as_fragment_but_short_answers_are_filled():
    primary = words(BEFORE, 1.0) + words(AFTER, 8.5)
    for sound in ("Sss.", "Shh", "S. T.", "Psst!", "Brr"):
        r = complete(primary, "parakeet", [Source("parakeet-pass2", primary[:8] + words(sound, 5.5) + primary[8:])])
        (g,) = r.completeness.gaps
        assert g.status == "rejected" and g.reason.startswith("fragment"), sound
    primary = words(BEFORE, 1.0) + words("That matches the printed timetable.", 8.5)
    for answer in ("Yes.", "No.", "Okay.", "GPS", "I see."):
        r = complete(primary, "parakeet", [Source("parakeet-pass2", primary[:8] + words(answer, 5.5) + primary[8:])])
        assert r.completeness.unconfirmed == 1 and r.completeness.rejected == 0, answer
    # found by two sources, a fragment is filled (both heard it)
    two = [Source(n, primary[:8] + words("Shh", 5.5) + primary[8:]) for n in ("parakeet-pass2", "whisperkit")]
    assert complete(primary, "parakeet", two).completeness.confirmed == 1


def test_the_same_phrase_from_two_sources_far_apart_in_one_silence_is_filled_once_and_confirmed():
    primary = words("so the first bus is late", 1.0) + words("and then we walk home now", 20.0)
    second = words("so the first bus is late", 1.02) + words("what if we take a taxi", 5.0) + words(
        "and then we walk home now", 20.03)
    cues = [Caption(start=0.0, end=4.0, text="so the first bus is late"),
            Caption(start=10.0, end=14.0, text="what if we take a taxi"),
            Caption(start=19.0, end=23.0, text="and then we walk home now")]
    r = complete(primary, "parakeet", [Source("parakeet-pass2", second), caption_source(cues)])
    (g,) = r.completeness.gaps
    assert g.sources == ["parakeet-pass2", "captions"] and g.status == "confirmed"
    assert " ".join(w.text for w in r.words if w.filled) == "what if we take a taxi"
    # two different phrases of two sources in one long silence stay two fills
    other = words("so the first bus is late", 1.0) + words("a taxi rank is nearby", 12.0) + words(
        "and then we walk home now", 20.0)
    r = complete(primary, "parakeet", [Source("parakeet-pass2", second), Source("whisperkit", other)],
                 speech_share=lambda a, b: 0.8)
    assert r.completeness.unconfirmed == 2 and r.completeness.filled_words == 11


def test_a_gap_is_confirmed_only_when_two_sources_agree_on_every_filled_word():
    primary = primary_without_phrase()
    agree = complete(primary, "parakeet", [Source("parakeet-pass2", full_source(0.3)), Source("whisperkit", full_source(-0.2))])
    assert agree.completeness.confirmed == 1
    # whisperkit heard something else in the same silence: both found a gap, but the words do not agree
    differ = words(BEFORE, 0.8) + words("Will the night express stop at Penford?", 5.0) + words(AFTER, 8.3)
    r = complete(primary, "parakeet", [Source("parakeet-pass2", full_source(0.3)), Source("whisperkit", differ)],
                 speech_share=lambda a, b: 0.8)
    (g,) = r.completeness.gaps
    assert g.status == "unconfirmed" and g.sources == ["parakeet-pass2", "whisperkit"] and "marlow" in g.reason
    assert all(w.confirmed is False for w in r.words if w.filled)
    assert (r.completeness.confirmed, r.completeness.unconfirmed) == (0, 1)
    marked = asr.marked_text(r.words)
    assert "[? " in marked and "[+ " not in marked
