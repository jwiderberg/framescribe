"""The parakeet stage's chunk loop with a fake model: context on both sides, the bare-chunk guard and its warning,
and the second pass on the offset cut grid."""

import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import parakeet_mlx
import pytest

from framescribe import doctor
from framescribe.chunking import Levels, read_wav, write_wav
from framescribe.config import Config
from framescribe.models import AudioFacts, Cut, IngestArtifact, Layout, Rect, VideoFacts
from framescribe.stages import asr_parakeet
from framescribe.stages.base import Context, StageError

RATE = 16000
PAUSES = (29.0, 59.0, 89.0, 119.0)  # 1 s pauses: the first pass cuts at 59.5 and 119.5, the second at 29.5 and 89.5


@dataclass
class Tok:
    text: str
    start: float
    end: float
    confidence: float = 1.0


class FakeModel:
    """One word per second of audio. Words carry punctuation only if `punctuate(lead, seconds)` says so, where `lead`
    is the length of digital silence (padding: the test audio has none) the decoded audio starts with."""

    def __init__(self, punctuate):
        self.punctuate = punctuate
        self.calls = []

    def transcribe(self, path):
        x, rate = read_wav(path)
        lead = int(np.argmax(x != 0)) / rate if (x != 0).any() else len(x) / rate
        self.calls.append((x, lead))
        marks = self.punctuate(lead, len(x) / rate)
        call = len(self.calls) - 1
        tokens = [Tok(f" c{call}w{i}{'.' if marks and i % 5 == 4 else ''}", t, t + 0.4)
                  for i, t in enumerate(np.arange(lead + 0.05, len(x) / rate - 0.5, 1.0))]
        return SimpleNamespace(sentences=[SimpleNamespace(tokens=tokens)])


def stage_context(tmp_path, seconds=150.0):
    rng = np.random.default_rng(0)
    x = rng.normal(0, 3000, int(seconds * RATE)).astype(np.int16)
    for t in PAUSES:  # quiet, but not digital silence
        x[int(t * RATE) : int((t + 1.0) * RATE)] = rng.normal(0, 3, RATE).astype(np.int16) | 1
    (tmp_path / "work").mkdir()
    write_wav(tmp_path / "work/audio.wav", x, RATE)
    ingest = IngestArtifact(source="v.mp4", duration=seconds, video=VideoFacts(width=1, height=1, fps=1, codec="h264"),
                            audio=AudioFacts(codec="aac", sample_rate=RATE, channels=1), audio_wav="work/audio.wav",
                            embedded_captions=[], layout=Layout(frame_w=1, frame_h=1, content_rect=Rect(x=0, y=0, w=1, h=1),
                                                                method="m", confidence=1))
    logs = []
    ctx = Context(config=Config(chunk_seconds=60.0, cut_search_seconds=10.0, chunk_max_seconds=70.0),
                  source=tmp_path / "v.mp4", run_dir=tmp_path, artifacts={"ingest": ingest}, stage="asr.parakeet",
                  emit=lambda event, **f: logs.append(f.get("message")) if event == "log" else None)
    return ctx, logs


def fake_mlx(monkeypatch, calls, previous_limit=123 * 1024 * 1024):
    """Replace the MLX memory functions with recorders, so no test touches the real allocator."""
    import mlx.core as mx

    in_force = [previous_limit]

    def set_cache_limit(limit):
        calls.append(("set_cache_limit", limit))
        old, in_force[0] = in_force[0], limit
        return old

    monkeypatch.setattr(mx, "set_cache_limit", set_cache_limit)
    monkeypatch.setattr(mx, "clear_cache", lambda: calls.append(("clear_cache",)))
    monkeypatch.setattr(mx, "reset_peak_memory", lambda: calls.append(("reset_peak_memory",)))
    monkeypatch.setattr(mx, "get_peak_memory", lambda: 3 * 1024 * 1024 * 1024)


def cached(monkeypatch, present):
    """Fake the local cache check; the guard sets HF_HUB_OFFLINE for the process, so restore it afterwards."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(doctor, "_model_files", lambda repo: {"model.safetensors": Path("m/model.safetensors")} if present else None)


def run_with(monkeypatch, tmp_path, model):
    loads = []
    cached(monkeypatch, True)
    monkeypatch.setattr(parakeet_mlx, "from_pretrained", lambda name: loads.append(name) or model)
    fake_mlx(monkeypatch, [])
    ctx, logs = stage_context(tmp_path)
    art = asr_parakeet.run(ctx)
    assert len(loads) == 1  # the model is loaded once for both passes
    return art, logs


def test_the_stage_loads_the_model_only_when_it_is_in_the_local_cache_and_then_offline(monkeypatch, tmp_path):
    cached(monkeypatch, True)
    monkeypatch.delenv("HF_HUB_OFFLINE")  # a host program's own environment: the stage must set offline mode itself
    loads = []
    monkeypatch.setattr(parakeet_mlx, "from_pretrained",
                        lambda name: loads.append((name, os.environ.get("HF_HUB_OFFLINE"))) or FakeModel(lambda lead, s: True))
    fake_mlx(monkeypatch, [])
    ctx, _ = stage_context(tmp_path)
    asr_parakeet.run(ctx)
    # the pinned snapshot's folder from the cache (Path("m/model.safetensors") in the fake), never the repository name,
    # which parakeet-mlx would resolve to the branch main
    assert loads == [("m", "1")]


def test_the_stage_without_the_model_in_the_local_cache_raises_and_never_calls_the_loader(monkeypatch, tmp_path):
    cached(monkeypatch, False)
    monkeypatch.delenv("HF_HUB_OFFLINE")
    loads = []
    monkeypatch.setattr(parakeet_mlx, "from_pretrained", lambda name: loads.append(name))
    fake_mlx(monkeypatch, [])
    ctx, _ = stage_context(tmp_path)
    with pytest.raises(StageError, match="framescribe setup"):
        asr_parakeet.run(ctx)
    assert loads == []
    assert os.environ["HF_HUB_OFFLINE"] == "1"  # offline even when it stops


def test_each_chunk_is_decoded_with_context_on_both_sides_whose_words_are_dropped(monkeypatch, tmp_path):
    model = FakeModel(lambda lead, seconds: True)
    art, _ = run_with(monkeypatch, tmp_path, model)
    assert [c.kind for c in art.cuts] == ["pause", "pause"]
    assert [c.time for c in art.cuts] == pytest.approx([59.5, 119.5], abs=0.02)
    source, _ = read_wav(tmp_path / "work/audio.wav")
    pad = round(asr_parakeet.PAD_SECONDS * RATE)
    first_pass = model.calls[:3]
    for (decoded, lead), a, b in zip(first_pass, [0.0, *(c.time for c in art.cuts)], [*(c.time for c in art.cuts), 150.0]):
        first = max(0, round((a - asr_parakeet.LEFT_CONTEXT_SECONDS) * RATE))  # none before the start
        last = min(len(source), round((b + asr_parakeet.RIGHT_CONTEXT_SECONDS) * RATE))  # none after the end
        assert lead == pytest.approx(asr_parakeet.PAD_SECONDS, abs=0.01)
        assert np.array_equal(decoded[pad:], source[first:last])
    # the words a chunk's decode finds in its context belong to the neighbouring chunks: none is kept from that decode
    for call, (a, b) in enumerate(zip([0.0, 59.5, 119.5], [59.5, 119.5, 150.0])):
        mine = [w for w in art.words if w.text.startswith(f"c{call}w")]
        assert mine and all(a <= w.start < b for w in mine)
    assert all(a.start < b.start for a, b in zip(art.words, art.words[1:]))
    assert [w for w in art.warnings if not w.startswith("pass 2: ")] == []


def test_second_pass_cuts_near_the_middle_of_the_first_pass_chunks(monkeypatch, tmp_path):
    art, _ = run_with(monkeypatch, tmp_path, FakeModel(lambda lead, seconds: True))
    second = art.second_pass
    assert second is not None and second.name == "parakeet-pass2"
    assert [c.time for c in second.cuts[:2]] == pytest.approx([29.5, 89.5], abs=0.02)
    # no pause in the middle of the last chunk: a cut between two words near it, and a warning
    assert second.cuts[2].kind == "no_pause" and abs(second.cuts[2].time - 134.75) <= asr_parakeet.NO_PAUSE_SHIFT_SECONDS
    assert any(w.startswith("pass 2: no pause near 134.75 s") for w in art.warnings)
    assert second.words and all(a.start <= b.start for a, b in zip(second.words, second.words[1:]))
    assert len(art.pass_seconds) == 2 and art.words != second.words


def test_offset_grid_puts_a_cut_inside_every_chunk_and_keeps_to_the_longest_chunk():
    x = np.random.default_rng(1).normal(0, 3000, 100 * RATE).astype(np.int16)
    levels = Levels(x, RATE)
    first = [Cut(time=t, kind="pause", level_db=-60.0, pause_seconds=0.5) for t in (38.0, 76.0, 80.0)]
    cuts = asr_parakeet.plan_offset_cuts(levels, first, 100.0, maximum=40.0)
    edges = [0.0, *(c.time for c in cuts), 100.0]
    assert all(b - a <= 40.0 for a, b in zip(edges, edges[1:]))
    first_edges = [0.0, 38.0, 76.0, 80.0, 100.0]
    assert all(any(a < c.time < b for c in cuts) for a, b in zip(first_edges, first_edges[1:]))
    assert asr_parakeet.plan_offset_cuts(levels, [], 30.0, maximum=40.0) == []


CHUNKS = BARE_CHUNKS = 3 + 4  # both passes; every chunk has well over BARE_MIN_WORDS words
RETRIES = len(asr_parakeet.retry_decodings(asr_parakeet.Decoding()))


def test_bare_chunk_is_retried_until_a_decode_is_punctuated(monkeypatch, tmp_path):
    pads = asr_parakeet.RETRY_PAD_SECONDS
    model = FakeModel(lambda lead, seconds: lead >= pads[-1] - 0.01)  # only the retry with the most silence punctuates
    art, logs = run_with(monkeypatch, tmp_path, model)
    tries = [d.pad for d in asr_parakeet.retry_decodings(asr_parakeet.Decoding())]
    assert len(model.calls) == CHUNKS + (tries.index(pads[-1]) + 1) * BARE_CHUNKS
    assert sum(1 for w in art.words if w.text.endswith(".")) > 15
    assert not any("without punctuation" in w for w in art.warnings)


def test_bare_chunk_is_retried_first_without_the_context_before_its_cut(monkeypatch, tmp_path):
    model = FakeModel(lambda lead, seconds: False)
    art, _ = run_with(monkeypatch, tmp_path, model)
    source, _ = read_wav(tmp_path / "work/audio.wav")
    a, b = art.cuts[0].time, art.cuts[1].time
    # the second chunk: first try, then without left context
    first_try, retry = (x for x, _ in model.calls[1 + RETRIES : 3 + RETRIES])
    left, right = asr_parakeet.LEFT_CONTEXT_SECONDS, asr_parakeet.RIGHT_CONTEXT_SECONDS
    assert np.array_equal(first_try, source[round((a - left) * RATE) : round((b + right) * RATE)])
    assert np.array_equal(retry, source[round(a * RATE) : round((b + right) * RATE)])


def test_chunk_that_stays_bare_is_retried_with_more_context_and_recorded_as_a_warning(monkeypatch, tmp_path):
    model = FakeModel(lambda lead, seconds: False)
    art, logs = run_with(monkeypatch, tmp_path, model)
    bare = [w for w in art.warnings if "without punctuation" in w]
    assert len(bare) == BARE_CHUNKS and bare[0].startswith("chunk 0.00-59.50 s: ")
    assert sum(w.startswith("pass 2: ") for w in bare) == 4
    assert all(any(w in (m or "") for m in logs) for w in bare)
    assert len(model.calls) == CHUNKS * (1 + RETRIES)  # first try and every retry
    # the first chunk's last try: RETRY_CONTEXT_SECONDS of context, no silence
    last, lead = model.calls[RETRIES]
    assert lead == pytest.approx(0.0, abs=0.01)
    assert len(last) / RATE == pytest.approx(59.5 + asr_parakeet.RETRY_CONTEXT_SECONDS, abs=0.02)
    assert [d.left for d in asr_parakeet.retry_decodings(asr_parakeet.Decoding())][:3] == list(asr_parakeet.RETRY_LEFT_SECONDS)


def test_a_word_decoded_on_both_sides_of_a_cut_is_kept_once():
    from framescribe.models import Word

    def w(text, start, end):
        return Word(text=text, start=start, end=end)

    # the chunk before the cut at 10.0 heard "the" in its context after the cut and timed it at 9.99
    words = [w("follows", 9.4, 9.9), w("the", 9.99, 10.08)]
    after_cut = [w("follows", 9.45, 9.92), w("the", 10.05, 10.12), w("narrow", 10.14, 10.5)]
    asr_parakeet.drop_seam_duplicates(words, after_cut, 10.0)
    assert [x.text for x in words] == ["follows"]
    # a word the next chunk does not keep (it starts before the cut there too) stays
    words = [w("follows", 9.4, 9.9), w("the", 9.99, 10.08)]
    asr_parakeet.drop_seam_duplicates(words, [w("the", 9.95, 10.05), w("narrow", 10.14, 10.5)], 10.0)
    assert [x.text for x in words] == ["follows", "the"]


class ScriptedModel:
    """Returns, per call, the words `script(call, seconds)` gives as (text, start) pairs, start relative to the
    decoded audio; records the length of every decoded audio."""

    def __init__(self, script):
        self.script = script
        self.seconds = []

    def transcribe(self, path):
        x, rate = read_wav(path)
        self.seconds.append(len(x) / rate)
        toks = [Tok(f" {t}", s, s + 0.3) for t, s in self.script(len(self.seconds) - 1, len(x) / rate)]
        return SimpleNamespace(sentences=[SimpleNamespace(tokens=toks)] if toks else [])


def noise(seconds, pauses=()):
    rng = np.random.default_rng(0)
    x = rng.normal(0, 3000, int(seconds * RATE)).astype(np.int16)
    for t, length in pauses:
        x[int(t * RATE) : int((t + length) * RATE)] = rng.normal(0, 3, int(length * RATE)).astype(np.int16) | 1
    return x


def decode_alone(tmp_path, x, model, planned=()):
    return asr_parakeet.decode_pass(model, x, RATE, Levels(x, RATE), list(planned), asr_parakeet.Decoding(), tmp_path,
                                    5.0)


def test_a_retry_never_replaces_a_decode_that_has_more_words(tmp_path):
    x = noise(16.0)
    bare = [(f"w{i}", 0.2 + i * 0.5) for i in range(28)]  # 28 words, no punctuation
    for retry in ([], [(f"r{i}.", 0.2 + i * 2.0) for i in range(5)]):  # retries: nothing, or 5 punctuated words
        model = ScriptedModel(lambda call, seconds: bare if call == 0 else retry)
        r = decode_alone(tmp_path, x, model)
        assert [w.text for w in r.words] == [t for t, _ in bare]
        assert len(model.seconds) == 1 + RETRIES  # no retry was good enough: every one was tried
        (warning,) = r.warnings
        assert warning.startswith("chunk 0.00-16.00 s: 28 words without punctuation kept")
    # a punctuated retry with as many words wins over the bare decode
    model = ScriptedModel(lambda call, seconds: bare if call == 0 else [(t + ",", s) for t, s in bare])
    r = decode_alone(tmp_path, x, model)
    assert len(r.words) == 28 and r.words[0].text == "w0," and r.warnings == [] and len(model.seconds) == 2


def test_best_attempt_prefers_most_words_then_punctuation_then_the_first():
    from framescribe.models import Word

    def ws(n, mark=""):
        return [Word(text=f"w{i}{mark}", start=i, end=i + 0.5) for i in range(n)]

    assert asr_parakeet.best_attempt([ws(28), []]) == 0
    assert asr_parakeet.best_attempt([ws(28), ws(27, ".")]) == 0
    assert asr_parakeet.best_attempt([ws(28), ws(28, "."), ws(28, ".")]) == 1
    assert asr_parakeet.best_attempt([ws(12), ws(29)]) == 1


def test_an_empty_chunk_with_speech_energy_is_retried_with_a_wider_window_then_warned(tmp_path):
    x = noise(60.0, pauses=[(29.5, 1.0), (44.0, 0.6)])  # the first pass cuts at 30.0 s
    planned = [Cut(time=30.0, kind="pause", level_db=-60.0, pause_seconds=1.0)]
    words = lambda seconds: [(f"w{i}.", 0.1 + i) for i in range(int(seconds) - 1)]  # noqa: E731
    # the second chunk's first decode comes back empty; the retry with more context before it has its words
    model = ScriptedModel(lambda call, seconds: [] if call == 1 else words(seconds))
    r = decode_alone(tmp_path, x, model, planned)
    assert len(model.seconds) == 3
    assert model.seconds[2] == pytest.approx(model.seconds[1] + 15.0 - asr_parakeet.LEFT_CONTEXT_SECONDS, abs=0.02)
    assert any(30.0 <= w.start < 60.0 for w in r.words) and r.warnings == []
    # every decode of it empty: kept empty, with a warning naming the chunk
    model = ScriptedModel(lambda call, seconds: [] if call >= 1 else words(seconds))
    r = decode_alone(tmp_path, x, model, planned)
    assert len(model.seconds) == 3 and not any(w.start >= 30.0 for w in r.words)
    (warning,) = r.warnings
    assert warning.startswith("chunk 30.00-60.00 s: no words, although") and "retry with 15.0 s of context" in warning
    # an empty chunk without speech energy is not retried
    model = ScriptedModel(lambda call, seconds: [] if call >= 1 else words(seconds))
    quiet_end = np.concatenate([noise(30.0), np.ones(30 * RATE, np.int16)])
    r = decode_alone(tmp_path, quiet_end, model, planned)
    assert len(model.seconds) == 2 and r.warnings == []


def test_the_empty_retry_window_reaches_back_to_a_pause():
    x = noise(100.0, pauses=[(50.0, 0.6)])
    how = asr_parakeet.empty_retry_decoding(asr_parakeet.Decoding(), Levels(x, RATE), 60.0, 90.0, 100.0)
    assert how.left == pytest.approx(60.0 - 50.3, abs=0.02) and how.right == asr_parakeet.RIGHT_CONTEXT_SECONDS
    start = asr_parakeet.empty_retry_decoding(asr_parakeet.Decoding(), Levels(x, RATE), 0.0, 30.0, 100.0)
    assert start.right == pytest.approx(asr_parakeet.RIGHT_CONTEXT_SECONDS + asr_parakeet.EMPTY_RETRY_MAX) and start.pad > 0


def test_the_first_word_after_a_cut_takes_its_casing_from_the_other_pass():
    from framescribe.models import Word

    def w(text, start):
        return Word(text=text, start=start, end=start + 0.3)

    cuts = [Cut(time=t, kind="pause", level_db=-60.0, pause_seconds=0.5) for t in (10.0, 20.0, 30.0, 40.0)]
    words = [w("going", 9.0), w("So", 10.2), w("then", 19.0), w("Then,", 20.1), w("we", 29.0), w("Okay", 30.3),
             w("see", 39.0), w("Paris", 40.2)]
    other = [w("going", 9.05), w("so", 10.25), w("then", 19.0), w("then", 20.15), w("we", 29.0), w("OK", 30.3),
             w("see", 39.0), w("Paris", 40.2)]
    assert asr_parakeet.arbitrate_cut_casing(words, cuts, other) == 2
    # recased where the other pass has the same word; "OK" is the same token but other letters: left alone
    assert [x.text for x in words] == ["going", "so", "then", "then,", "we", "Okay", "see", "Paris"]
    # no word of the other pass at that time: left as it is
    lone = [w("So", 10.2)]
    assert asr_parakeet.arbitrate_cut_casing(lone, cuts[:1], [w("so", 11.5)]) == 0 and lone[0].text == "So"
    # only casing ever changes: a different word at that time is not taken
    other_word = [w("Go", 10.2)]
    assert asr_parakeet.arbitrate_cut_casing(other_word, cuts[:1], [w("so", 10.2)]) == 0 and other_word[0].text == "Go"


def test_the_stage_sets_the_configured_cache_limit_before_decoding_and_restores_the_previous_one_after(monkeypatch, tmp_path):
    calls: list[tuple] = []
    fake_mlx(monkeypatch, calls, previous_limit=123 * 1024 * 1024)

    class Recording(FakeModel):
        def transcribe(self, path):
            calls.append(("transcribe",))
            return super().transcribe(path)

    monkeypatch.setattr(parakeet_mlx, "from_pretrained", lambda name: Recording(lambda lead, seconds: True))
    ctx, logs = stage_context(tmp_path)
    ctx.config = Config(chunk_seconds=60.0, cut_search_seconds=10.0, chunk_max_seconds=70.0, mlx_cache_limit_mb=256)
    asr_parakeet.run(ctx)
    names = [c[0] for c in calls]
    assert calls[0] == ("set_cache_limit", 256 * 1024 * 1024)  # set before anything is decoded
    assert names.index("transcribe") > 0 and names.count("transcribe") >= 6
    assert calls[-1] == ("set_cache_limit", 123 * 1024 * 1024)  # the previous limit is back at the end ...
    assert names[-2] == "clear_cache"  # ... after the cache was emptied
    assert names.count("set_cache_limit") == 2
    assert max(i for i, n in enumerate(names) if n == "transcribe") < len(names) - 2  # nothing is decoded after the restore
    # one line at the start with the limit in force, one at the end with the peak; the peak also goes to run.json's metrics
    assert [m for m in logs if m and m.startswith("MLX cache limit")] == ["MLX cache limit 256 MB (was 123 MB)"]
    assert [m for m in logs if m and m.startswith("MLX peak memory")] == ["MLX peak memory 3072 MB"]
    assert ctx.metrics == {"mlx_peak_mb": 3072.0, "mlx_cache_limit_mb": 256}


def test_the_previous_cache_limit_is_restored_when_decoding_fails(monkeypatch, tmp_path):
    calls: list[tuple] = []
    fake_mlx(monkeypatch, calls, previous_limit=7 * 1024 * 1024)
    stats: dict = {}
    with pytest.raises(RuntimeError):
        with asr_parakeet.mlx_memory_scope(64, lambda message: None, stats):
            raise RuntimeError("decode failed")
    assert calls[-1] == ("set_cache_limit", 7 * 1024 * 1024)
    assert stats["mlx_cache_limit_mb"] == 64


def test_a_negative_cache_limit_is_refused(tmp_path):
    ctx, _ = stage_context(tmp_path)
    ctx.config = Config(mlx_cache_limit_mb=-1)
    with pytest.raises(asr_parakeet.StageError):
        asr_parakeet.run(ctx)


def test_the_warning_of_a_cut_without_a_pause_says_how_it_was_placed(tmp_path):
    x = noise(70.0)  # sound all the way, no pause
    planned = [Cut(time=30.0, kind="no_pause", level_db=-20.0, pause_seconds=0.0)]
    pad = asr_parakeet.PAD_SECONDS  # the first decode starts at 0 s after this much leading silence

    def first_warning(words):
        r = decode_alone(tmp_path, x, ScriptedModel(lambda call, seconds: words if call == 0 else []), planned)
        return r.cuts[0], r.warnings[0]

    # two touching words (ScriptedModel's words are 0.3 s long): a gap to cut in, 0 s wide
    cut, warning = first_warning([("a.", 29.7 + pad), ("b.", 30.0 + pad)])
    assert cut.time == pytest.approx(30.0, abs=0.01) and "between two words (0.00 s apart" in warning
    # no word anywhere near: the cut stays where it was planned
    cut, warning = first_warning([("a.", 5.0 + pad)])
    assert cut.time == pytest.approx(30.0) and "where the decode has no two words to cut between" in warning
    # a lone word starting just after the planned cut: the cut moves before it, never later
    cut, warning = first_warning([("a.", 30.1 + pad)])
    assert cut.time == pytest.approx(30.1 - asr_parakeet.LONE_WORD_MARGIN_SECONDS, abs=0.01)
    assert "just before a lone word" in warning
