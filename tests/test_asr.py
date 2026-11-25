import importlib.util
import json
from pathlib import Path

import pytest

from framescribe.errors import WordOrderError
from framescribe.models import AsrArtifact, Caption, Word
from framescribe.stages.asr import assign_speaker, build_transcript
from framescribe.stages.asr_import import import_transcript
from framescribe.stages.asr_whisperkit import parse_report
from framescribe.stages.base import StageError
from framescribe.words import segment_sentences

FIXTURES = Path(__file__).parent / "fixtures"


def cap(start, end, speaker, text="x"):
    return Caption(start=start, end=end, speaker=speaker, text=text)


def test_speaker_with_most_overlap_wins():
    caps = [cap(0, 4, "A"), cap(4, 8, "B"), cap(8, 12, "B")]
    assert assign_speaker(2.0, 6.5, caps) == "B"  # 2.0 s of A, 2.5 s of B
    assert assign_speaker(0.5, 3.0, caps) == "A"
    assert assign_speaker(3.5, 9.0, caps) == "B"


def test_speaker_overlap_is_summed_per_speaker():
    caps = [cap(0, 2, "A"), cap(2, 3, "B"), cap(3, 5, "A")]
    assert assign_speaker(0.0, 5.0, caps) == "A"


def test_segment_between_captions_takes_the_nearest_speaker_within_one_bucket():
    caps = [cap(0, 4, "A"), cap(12, 16, "B")]
    assert assign_speaker(5.0, 5.5, caps) == "A"  # 1.0 s from A, 6.5 s from B
    assert assign_speaker(10.0, 10.5, caps) == "B"
    assert assign_speaker(8.0, 8.5, caps) == "B"  # 4.0 s from A, 3.5 s from B
    assert assign_speaker(20.5, 21.0, caps) is None


def test_no_overlap_or_no_named_speaker_gives_none():
    assert assign_speaker(10, 11, [cap(0, 4, "A")]) is None
    assert assign_speaker(0, 4, [cap(0, 4, None)]) is None
    assert assign_speaker(0, 4, []) is None


def asr(backend, text_words):
    words = [Word(text=t, start=i * 0.5, end=i * 0.5 + 0.4) for i, t in enumerate(text_words)]
    return AsrArtifact(backend=backend, model="m", language=None, words=words, segments=segment_sentences(words),
                       audio_seconds=10.0, wall_seconds=1.0)


def test_build_transcript_keeps_words_and_alternates():
    primary = asr("parakeet", "hello there. how are you?".split())
    other = asr("whisperkit", "hello there. how are you".split())
    t = build_transcript(primary, [other])
    assert t.primary == "parakeet" and t.sources == ["parakeet", "whisperkit"]
    assert [s.text for s in t.segments] == ["hello there.", "how are you?"]
    assert [x.text for x in t.segments[1].words] == ["how", "are", "you?"]
    assert [s.id for s in t.segments] == [1, 2]
    assert list(t.alternates) == ["whisperkit"]
    assert [s.text for s in t.alternates["whisperkit"]] == ["hello there.", "how are you"]


def test_parse_whisperkit_report_fixture():
    report = json.loads((FIXTURES / "whisperkit_report.json").read_text())
    words, dropped, _ = parse_report(report, audio_seconds=report["timings"]["inputAudioSeconds"])
    assert dropped == []
    assert len(words) > 30
    assert all(w.text == w.text.strip() and w.text for w in words)
    assert not any(w.text.startswith("<|") for w in words)
    assert all(a.start <= b.start and a.end <= b.start + 1e-9 for a, b in zip(words, words[1:]))
    assert words[-1].end <= report["timings"]["inputAudioSeconds"]
    # the report's own words, in order, are what we got back (punctuation fragments aside)
    raw = "".join(w["word"].strip() for seg in report["segments"] for w in seg["words"] if not w["word"].strip().startswith("<|"))
    assert "".join(w.text for w in words) == raw


def test_whisperkit_window_restart_keeps_the_real_words_and_drops_the_hallucinated_tail():
    # Synthetic 0-60 s report (tests/fixtures/make_whisperkit_fixtures.py): window A (seek 24 s) has 16 real words up to
    # 37 s and then two hallucinated words, "Yes." (51.31) and "Right." (52.74); window B (seek 40 s) restarts at
    # 40.90 s with "Right." and 46 more words: 47 in all, 98 words kept of the report's 100.
    report = json.loads((FIXTURES / "whisperkit_restart_report.json").read_text())
    words, dropped, _ = parse_report(report, audio_seconds=60.0)
    assert [(w.text, round(w.start, 2)) for w in dropped] == [("Yes.", 51.31), ("Right.", 52.74)]
    assert len(words) == 98
    window = [w for w in words if 40.0 <= w.start]
    assert len(window) == 47 and window[0].text == "Right." and round(window[0].start, 2) == 40.90
    assert len({w.start for w in window}) == 47  # true, distinct timings: no pile-up on one instant
    assert all(w.end > w.start for w in window[:-1])
    assert all(a.start <= b.start and a.end <= b.start + 1e-9 for a, b in zip(words, words[1:]))


def test_whisperkit_fixtures_are_what_their_generator_writes():
    spec = importlib.util.spec_from_file_location("make_whisperkit_fixtures", FIXTURES / "make_whisperkit_fixtures.py")
    maker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(maker)
    for name, build in (("whisperkit_report.json", maker.build_report),
                        ("whisperkit_restart_report.json", maker.build_restart_report)):
        assert json.loads((FIXTURES / name).read_text()) == build(), f"{name}: rerun make_whisperkit_fixtures.py"


def seg(seek, *words):
    return {"seek": seek * 16000, "words": [{"word": f" {w}", "start": s, "end": s + 0.4} for w, s in words]}


@pytest.mark.parametrize("report", [
    # a later segment at 0.5 s with no window information: before, all six earlier words were dropped silently
    {"segments": [{"words": [{"word": f" w{i}", "start": float(i), "end": i + 0.5} for i in range(1, 7)]},
                  {"words": [{"word": " late", "start": 0.5, "end": 0.9}]}]},
    # a window at 30 s whose words start at 0.5 s, before the window itself
    {"segments": [seg(0, ("a", 1.0), ("b", 5.0)), seg(30, ("late", 0.5))]},
    # a restart reaching into a window older than the one just before it
    {"segments": [seg(0, ("a", 1.0), ("b", 25.0)), seg(20, ("c", 26.0), ("d", 40.0)), seg(24, ("e", 24.5))]},
    # the same window going backwards is not a restart
    {"segments": [seg(0, ("a", 1.0), ("b", 5.0)), seg(0, ("c", 3.0))]},
])
def test_whisperkit_restart_that_reaches_beyond_the_window_before_it_raises(report):
    with pytest.raises(StageError, match="WhisperKit"):
        parse_report(report, audio_seconds=60.0)


def test_whisperkit_restart_inside_the_window_before_it_drops_that_windows_tail():
    report = {"segments": [seg(0, ("a", 1.0), ("b", 20.0), ("Okay.", 27.0)), seg(18, ("b", 19.9), ("c", 22.0))]}
    words, dropped, _ = parse_report(report, audio_seconds=60.0)
    assert [w.text for w in words] == ["a", "b", "c"] and [w.text for w in dropped] == ["b", "Okay."]


def test_whisperkit_disorder_inside_one_segment_raises():
    report = {"segments": [{"words": [{"word": " a", "start": 5.0, "end": 5.5}, {"word": " b", "start": 3.0, "end": 3.5}]}]}
    with pytest.raises(WordOrderError):
        parse_report(report, audio_seconds=10.0)


def test_whisperkit_report_without_word_timings_fails_loudly():
    with pytest.raises(StageError, match="word"):
        parse_report({"segments": [{"text": "hi"}]}, audio_seconds=1.0)


def test_import_srt_gives_segments_with_speakers_and_no_words(tmp_path):
    f = tmp_path / "t.srt"
    f.write_text("1\n00:00:01,000 --> 00:00:03,000\n(Ann)\nHello.\n\n2\n00:00:03,000 --> 00:00:05,000\nWorld.\n")
    t = import_transcript(f)
    assert t.primary == "import" and t.sources == ["import:t.srt"]
    assert [(s.id, s.start, s.end, s.speaker, s.text, s.words) for s in t.segments] == [
        (1, 1.0, 3.0, "Ann", "Hello.", []),
        (2, 3.0, 5.0, None, "World.", []),
    ]


def test_import_vtt_and_json_roundtrip(tmp_path):
    vtt = tmp_path / "t.vtt"
    vtt.write_text("WEBVTT\n\n00:01.000 --> 00:02.000\n<v Bo>Hi</v>\n")
    assert import_transcript(vtt).segments[0].speaker == "Bo"
    original = import_transcript(vtt)
    js = tmp_path / "t.json"
    js.write_text(original.model_dump_json())
    assert import_transcript(js) == original


@pytest.mark.parametrize("name,content,match", [
    ("missing.srt", None, "not found"),
    ("t.txt", "hello", "unsupported"),
    ("empty.srt", "", "no cues"),
    ("bad.json", "{}", "not a framescribe transcript"),
])
def test_import_errors_are_loud(tmp_path, name, content, match):
    f = tmp_path / name
    if content is not None:
        f.write_text(content)
    with pytest.raises(StageError, match=match):
        import_transcript(f)


def test_whisperkit_words_just_past_the_audio_end_are_clamped_and_later_ones_dropped():
    # WhisperKit's last window can overshoot the audio: 28-190 ms past the end made the whole run fail before
    report = {"segments": [seg(0, ("a", 1.0), ("b", 59.9), ("c", 60.19)), seg(0, ("late", 60.8), ("later", 61.0))]}
    words, dropped, past_end = parse_report(report, audio_seconds=60.0)
    assert [(w.text, w.start, w.end) for w in words[-2:]] == [("b", 59.9, 60.0), ("c", 60.0, 60.0)]
    assert dropped == [] and [w.text for w in past_end] == ["late", "later"]
    too_many = {"segments": [seg(0, ("a", 1.0), *((f"x{i}", 61.0 + i) for i in range(6)))]}
    with pytest.raises(WordOrderError, match="after the audio end"):
        parse_report(too_many, audio_seconds=60.0)
