"""End to end on a short synthetic video with real speech (macOS `say`) and the real parakeet model."""

import difflib
import json
import random
import re
import shutil
import subprocess
import wave

import numpy as np
import pytest
from conftest import encode_frames, letterbox_frames

from framescribe.chunking import read_wav
from framescribe.config import Config
from framescribe.models import AsrArtifact, IngestArtifact, TranscriptArtifact
from framescribe.pipeline import run_pipeline

SPEECH = "Hello, this is a short test of the transcription pipeline. The quick brown fox jumps over the lazy dog."
EXPECTED_WORDS = {"hello", "short", "test", "transcription", "pipeline", "quick", "brown", "fox", "lazy", "dog"}

pytestmark = [pytest.mark.slow, pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS `say`")]


def make_speech_video(directory, text, name="speech talk.mp4", caption_speaker="Test Speaker"):
    aiff = directory / "speech.aiff"
    subprocess.run(["say", "-o", str(aiff), text], check=True)
    seconds = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(aiff)]).decode())
    srt = directory / "caps.srt"
    end = int(seconds) + 1
    spoken = re.sub(r"\s+", " ", re.sub(r"\[\[[^\]]*\]\]", " ", text)).strip()  # without `say`'s pause commands
    srt.write_text(f"1\n00:00:00,000 --> 00:{end // 60:02d}:{end % 60:02d},000\n({caption_speaker})\n{spoken}\n")
    video = directory / name
    encode_frames(letterbox_frames(seconds + 1), 1280, 720, video,
                  extra_inputs=["-i", str(aiff), "-i", str(srt)],
                  extra_args=["-map", "0:v", "-map", "1:a", "-map", "2", "-c:s", "mov_text"])
    return video, seconds


@pytest.fixture(scope="module")
def speech_video(tmp_path_factory):
    return make_speech_video(tmp_path_factory.mktemp("e2e"), SPEECH)[0]


def test_speech_video_end_to_end(speech_video, tmp_path):
    cfg = Config(asr="auto", layout_samples=6)  # no WhisperKit configured: must be skipped, with a reason
    out = tmp_path / "out"
    rec, run_dir = run_pipeline(speech_video, out, cfg)
    assert run_dir == out / "speech talk"

    assert [(s.stage, s.status) for s in rec.stages] == [
        ("ingest", "miss"), ("asr.parakeet", "miss"), ("asr", "miss"), ("visual", "miss"), ("ocr", "miss"), ("report", "miss"),
        ("asr.whisperkit", "skipped")]
    assert "no CLI configured" in rec.stages[-1].reason

    ingest = IngestArtifact.model_validate_json((run_dir / "work/ingest.json").read_text())
    assert ingest.layout.content_rect.model_dump() == pytest.approx({"x": 0, "y": 102, "w": 960, "h": 516}, abs=4)
    assert len(ingest.layout.other_rects) == 1
    assert [c.speaker for c in ingest.embedded_captions] == ["Test Speaker"]
    with wave.open(str(run_dir / "work/audio.wav")) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)

    asr = AsrArtifact.model_validate_json((run_dir / "work/asr.parakeet.json").read_text())
    heard = {re.sub(r"\W", "", w.text.lower()) for w in asr.words}
    assert len(EXPECTED_WORDS & heard) >= 8, heard
    assert all(a.start <= b.start for a, b in zip(asr.words, asr.words[1:]))
    assert 0 <= asr.words[0].start and asr.words[-1].end <= asr.audio_seconds

    transcript = TranscriptArtifact.model_validate_json((run_dir / "work/transcript.json").read_text())
    assert transcript.primary == "parakeet"
    assert transcript.segments and {s.speaker for s in transcript.segments} == {"Test Speaker"}
    assert "Test Speaker:" in (run_dir / "transcript.md").read_text()
    assert json.loads((run_dir / "run.json").read_text())["stages"][-1]["status"] == "skipped"

    rec2, _ = run_pipeline(speech_video, out, cfg)
    assert {s.status for s in rec2.stages if s.stage != "asr.whisperkit"} == {"hit"}
    assert rec2.total_wall_seconds < 10


KEYWORDS = ["banana", "tomato", "elephant", "bicycle", "mountain", "guitar", "window", "pencil"]
PAUSE = "[[slnc 700]]"  # short, so speech resumes ~0.35 s after a cut in its centre
LONG_SPEECH = f" {PAUSE} ".join(f"Please remember the {k} for later today." for k in KEYWORDS)


def norm(text):
    return re.sub(r"[^a-z0-9']", "", text.lower())


def test_multi_chunk_parakeet_has_exactly_the_spoken_words_and_cuts_in_pauses(tmp_path):
    video, seconds = make_speech_video(tmp_path, LONG_SPEECH, "long speech.mp4")
    assert seconds > 20
    events = []
    cfg = Config(asr="parakeet", layout_samples=4, chunk_seconds=7.0, cut_search_seconds=2.5, chunk_max_seconds=12.0)
    _, run_dir = run_pipeline(video, tmp_path / "out", cfg, sink=events.append)
    asr = AsrArtifact.model_validate_json((run_dir / "work/asr.parakeet.json").read_text())

    assert len(asr.cuts) >= 2 and {c.kind for c in asr.cuts} == {"pause"} and asr.warnings == []
    notes = [e["note"] for e in events if e["event"] == "stage_progress" and e["stage"] == "asr.parakeet"]
    n = len(asr.cuts) + 1
    assert all(any(note.endswith(f"pass 1, chunk {i}/{n}") for note in notes) for i in range(1, n + 1))

    # each cut is at a pause: almost no energy in the 100 ms around it, as the artifact records
    samples, rate = read_wav(run_dir / "work/audio.wav")
    for cut in asr.cuts:
        around = samples[int((cut.time - 0.05) * rate) : int((cut.time + 0.05) * rate)].astype(float)
        assert np.sqrt((around**2).mean()) < 300, cut
        assert cut.level_db < -50 and cut.pause_seconds >= 0.4

    # every word, once, in order: a chunk that lost or doubled audio at its start would show here
    assert [norm(w.text) for w in asr.words] == [norm(x) for x in LONG_SPEECH.replace(PAUSE, "").split()]
    assert all(a.start <= b.start and a.end <= b.start + 1e-9 for a, b in zip(asr.words, asr.words[1:]))
    assert all(w.end - w.start > 0 for w in asr.words)


def continuous_text(phrases, seed=7):
    """Run-on speech without punctuation: `say` leaves only ~0.3 s between some words, nowhere longer."""
    rng = random.Random(seed)
    adj = "purple silver gentle noisy ancient frozen bright hollow crooked velvet rusty golden narrow sleepy bitter".split()
    noun = ("elephant bicycle mountain guitar window pencil lantern harbor blanket village compass orchard tunnel "
            "feather ladder cabinet monkey garden river bottle").split()
    verb = "carries follows paints watches borrows repairs measures chases covers lifts".split()
    place = "Lisbon Toronto Nairobi Helsinki Santiago Dublin Osaka Vienna Cairo Denver".split()
    return " ".join(f"the {rng.choice(adj)} {rng.choice(noun)} {rng.choice(verb)} the {rng.choice(adj)} "
                    f"{rng.choice(noun)} near {rng.choice(place)} and then" for _ in range(phrases))


def speech_only_video(directory, text, name):
    """A video with tiny frames (fast to encode) and `say` audio."""
    aiff = directory / "speech.aiff"
    subprocess.run(["say", "-o", str(aiff), text], check=True)
    seconds = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(aiff)]).decode())
    video = directory / name
    frames = (np.full((36, 64, 3), 128, np.uint8) for _ in range(int((seconds + 1) * 10)))
    encode_frames(frames, 64, 36, video, extra_inputs=["-i", str(aiff)], extra_args=["-map", "0:v", "-map", "1:a"])
    return video


def seam_damage(asr, text, run_dir=None):
    """Words dropped, added, split or merged next to a cut (two words before it, three after), compared with the
    spoken text. With `run_dir`, a difference counts only where the same model's decode of the audio without a cut
    nearby (24 s centred on the cut) does not show it too: the synthetic voice is misheard now and then wherever it
    is ("Nairobi and" heard as one word by a decode without any cut), and only what the cut changes is damage. A
    word misheard as another single word is a recognition error, not damage done by the cut.

    It also asserts what the casing rule guarantees at the cuts (_casing_damage). That is not "no capital without a
    sentence stop": the model itself leaves out a full stop now and then, and a cut word without a counterpart in the
    other pass keeps its own casing."""
    casing = _casing_damage(asr)
    assert casing == [], f"casing at a cut differs from the other pass's, which has the same word there: {casing}"
    against_text = _damage(asr.words, asr.cuts, [norm(x) for x in text.split()])
    if run_dir is None or not against_text:
        return against_text
    from parakeet_mlx import from_pretrained

    from framescribe.chunking import write_wav
    from framescribe.words import words_from_tokens

    model = from_pretrained("mlx-community/parakeet-tdt-0.6b-v3")
    samples, rate = read_wav(run_dir / "work/audio.wav")
    damaged = {d[0] for d in against_text}
    damage = []
    for cut in asr.cuts:
        if round(cut.time, 2) not in damaged:
            continue
        lo = max(0.0, cut.time - 12.0)
        write_wav(run_dir / "work/reference.wav", samples[int(lo * rate) : int((cut.time + 12.0) * rate)], rate)
        ref = [w for w in words_from_tokens(s.tokens for s in model.transcribe(run_dir / "work/reference.wav").sentences)
               if abs(w.start + lo - cut.time) <= 4.0]
        mine = [w for w in asr.words if abs(w.start - cut.time) <= 4.0]
        if _damage(mine, [cut], [norm(w.text) for w in ref]):  # the uncut decode differs as well: the cut did it
            damage += [d for d in against_text if d[0] == round(cut.time, 2)]
    return damage


def _casing_damage(asr):
    """Cuts at which the rule of arbitrate_cut_casing does not hold: the first word after the cut has a counterpart
    in the other pass (the same normalised word starting within CASE_MATCH_SECONDS; the nearest one, as in the stage),
    is not a filler (it has comparison tokens) and the two have the same letters, yet their casing differs.
    A counterpart that is itself the first word after a cut of the other pass may have been recased by that pass, so
    those cuts are not judged."""
    from framescribe.completeness import normalise
    from framescribe.stages.asr_parakeet import CASE_MATCH_SECONDS, _letters_casing

    words, other = asr.words, asr.second_pass.words
    recased = {next(i for i, w in enumerate(other) if w.start >= c.time) for c in asr.second_pass.cuts
               if any(w.start >= c.time for w in other)}
    damage = []
    for cut in asr.cuts:
        k = next((i for i, w in enumerate(words) if w.start >= cut.time), None)
        if k is None or not normalise(words[k].text):
            continue
        w = words[k]
        matches = [(abs(o.start - w.start), j) for j, o in enumerate(other)
                   if abs(o.start - w.start) <= CASE_MATCH_SECONDS and normalise(o.text) == normalise(w.text)]
        if not matches:
            continue
        _, j = min(matches)
        expected = _letters_casing(w.text, other[j].text)
        if j not in recased and expected is not None and expected != w.text:
            damage.append((round(cut.time, 2), w.text, other[j].text))
    return damage


def _damage(words, cuts, spoken):
    heard = [norm(w.text) for w in words]
    ops = difflib.SequenceMatcher(None, spoken, heard, autojunk=False).get_opcodes()
    damage = []
    for cut in cuts:
        k = max(i for i, w in enumerate(words) if w.start < cut.time)
        near = range(k - 2, k + 4)
        for tag, i1, i2, j1, j2 in ops:
            touches = any(j in near for j in range(j1, max(j2, j1 + 1)))
            if touches and tag != "equal" and not (tag == "replace" and i2 - i1 == j2 - j1):
                damage.append((round(cut.time, 2), tag, spoken[i1:i2], heard[j1:j2]))
    return damage


def test_continuous_speech_is_cut_without_dropping_or_splitting_words(tmp_path):
    # 350 s of run-on speech. Before, the cut at 124.99 s (quietest sample, nearest the target) lost a word:
    # "and then the silver" became "and the || Silver".
    text = continuous_text(130)
    video = speech_only_video(tmp_path, text, "continuous.mp4")
    _, run_dir = run_pipeline(video, tmp_path / "out", Config(asr="parakeet", layout_samples=2))
    asr = AsrArtifact.model_validate_json((run_dir / "work/asr.parakeet.json").read_text())
    assert len(asr.cuts) >= 2 and {c.kind for c in asr.cuts} == {"pause"}
    assert seam_damage(asr, text, run_dir) == []


def test_continuous_speech_without_any_pause_is_cut_between_words(tmp_path, monkeypatch):
    # With no pause at all, a cut must fall between two words found by a decode that spans it, and say so.
    from framescribe import chunking

    monkeypatch.setattr(chunking, "MIN_PAUSE_SECONDS", 60.0)  # no quiet run qualifies as a pause
    text = continuous_text(16, seed=3)
    video = speech_only_video(tmp_path, text, "nopause.mp4")
    cfg = Config(asr="parakeet", layout_samples=2, chunk_seconds=11.0, cut_search_seconds=2.0, chunk_max_seconds=16.0)
    _, run_dir = run_pipeline(video, tmp_path / "out", cfg)
    asr = AsrArtifact.model_validate_json((run_dir / "work/asr.parakeet.json").read_text())
    assert len(asr.cuts) >= 3 and {c.kind for c in asr.cuts} == {"no_pause"}
    assert len([w for w in asr.warnings if w.startswith("no pause near")]) == len(asr.cuts)
    assert seam_damage(asr, text, run_dir) == []
