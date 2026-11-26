"""Pipeline behaviour on a tiny synthetic video, with an imported transcript so no ASR model runs."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from framescribe.cli import app
from framescribe.config import Config
from framescribe.models import IngestArtifact, TranscriptArtifact
from framescribe.pipeline import build_plan, run_pipeline
from framescribe.stages import ingest
from framescribe.stages.base import StageError


@pytest.fixture
def srt(tmp_path) -> Path:
    f = tmp_path / "talk.srt"
    f.write_text("1\n00:00:00,500 --> 00:00:01,500\n(Ann)\nHello there.\n\n2\n00:00:01,500 --> 00:00:02,500\nSecond cue.\n")
    return f


def statuses(record) -> dict[str, str]:
    return {s.stage: s.status for s in record.stages}


def test_first_run_then_full_cache_hit(letterbox_video, srt, tmp_path):
    out = tmp_path / "out"
    cfg = Config(transcript=srt, layout_samples=4)
    rec, run_dir = run_pipeline(letterbox_video, out, cfg)
    assert statuses(rec) == {"ingest": "miss", "asr": "miss", "visual": "miss", "ocr": "miss", "report": "miss"}
    assert run_dir == out / "letterbox"
    for name in ("work/ingest.json", "work/transcript.json", "work/audio.wav", "transcript.md", "run.json"):
        assert (run_dir / name).is_file(), name
    rec2, _ = run_pipeline(letterbox_video, out, cfg)
    assert statuses(rec2) == {"ingest": "hit", "asr": "hit", "visual": "hit", "ocr": "hit", "report": "hit"}
    assert json.loads((run_dir / "run.json").read_text())["stages"][0]["status"] == "hit"


def test_import_yields_transcript_artifact_without_asr(letterbox_video, srt, tmp_path):
    rec, run_dir = run_pipeline(letterbox_video, tmp_path, Config(transcript=srt, layout_samples=4))
    t = TranscriptArtifact.model_validate_json((run_dir / "work/transcript.json").read_text())
    assert t.primary == "import"
    assert [(s.text, s.speaker) for s in t.segments] == [("Hello there.", "Ann"), ("Second cue.", None)]
    assert not (run_dir / "work/asr.parakeet.json").exists()
    assert "Ann: Hello there." in (run_dir / "transcript.md").read_text()


def test_config_change_invalidates_that_stage_and_downstream(letterbox_video, srt, tmp_path):
    cfg = Config(transcript=srt, layout_samples=4)
    run_pipeline(letterbox_video, tmp_path, cfg)
    rec, run_dir = run_pipeline(letterbox_video, tmp_path, replace(cfg, content_rect=(0, 102, 960, 516)))
    assert statuses(rec) == {"ingest": "miss", "asr": "miss", "visual": "miss", "ocr": "miss", "report": "miss"}  # asr's own config is unchanged; upstream key changed
    assert IngestArtifact.model_validate_json((run_dir / "work/ingest.json").read_text()).layout.method == "manual"


def test_config_field_of_downstream_stage_leaves_upstream_cached(letterbox_video, srt, tmp_path):
    cfg = Config(transcript=srt, layout_samples=4)
    run_pipeline(letterbox_video, tmp_path, cfg)
    other = srt.with_name("other.srt")
    other.write_text(srt.read_text().replace("Hello", "Howdy"))
    rec, _ = run_pipeline(letterbox_video, tmp_path, replace(cfg, transcript=other))
    assert statuses(rec) == {"ingest": "hit", "asr": "miss", "visual": "hit", "ocr": "hit", "report": "miss"}


def test_changed_transcript_content_at_same_path_invalidates_asr(letterbox_video, srt, tmp_path):
    cfg = Config(transcript=srt, layout_samples=4)
    run_pipeline(letterbox_video, tmp_path, cfg)
    srt.write_text(srt.read_text().replace("Hello", "Howdy"))
    rec, _ = run_pipeline(letterbox_video, tmp_path, cfg)
    assert statuses(rec) == {"ingest": "hit", "asr": "miss", "visual": "hit", "ocr": "hit", "report": "miss"}


def test_stage_version_change_invalidates_downstream_too(letterbox_video, srt, tmp_path, monkeypatch):
    cfg = Config(transcript=srt, layout_samples=4)
    run_pipeline(letterbox_video, tmp_path, cfg)
    monkeypatch.setattr(ingest, "STAGE", replace(ingest.STAGE, version=ingest.STAGE.version + 1))
    rec, _ = run_pipeline(letterbox_video, tmp_path, cfg)
    assert statuses(rec) == {"ingest": "miss", "asr": "miss", "visual": "miss", "ocr": "miss", "report": "miss"}


def test_upstream_source_change_invalidates_everything(letterbox_video, fullframe_video, srt, tmp_path):
    cfg = Config(transcript=srt, layout_samples=4)
    run_pipeline(letterbox_video, tmp_path, cfg)
    copy = tmp_path / "letterbox.mp4"
    copy.write_bytes(fullframe_video.read_bytes())  # different content, same stem, so same run directory
    rec, _ = run_pipeline(copy, tmp_path / "x", cfg)
    assert set(statuses(rec).values()) == {"miss"}
    rec, _ = run_pipeline(copy, tmp_path / "x", cfg)
    assert set(statuses(rec).values()) == {"hit"}
    copy.write_bytes(letterbox_video.read_bytes())
    rec, _ = run_pipeline(copy, tmp_path / "x", cfg)
    assert set(statuses(rec).values()) == {"miss"}


def test_force_stage_recomputes_it_and_everything_downstream(letterbox_video, srt, tmp_path):
    cfg = Config(transcript=srt, layout_samples=4)
    run_pipeline(letterbox_video, tmp_path, cfg)
    rec, _ = run_pipeline(letterbox_video, tmp_path, cfg, force=["asr"])
    assert statuses(rec) == {"ingest": "hit", "asr": "forced", "visual": "hit", "ocr": "hit", "report": "forced"}
    rec, _ = run_pipeline(letterbox_video, tmp_path, cfg, force=["ingest"])
    assert statuses(rec) == {"ingest": "forced", "asr": "forced", "visual": "forced", "ocr": "forced", "report": "forced"}
    rec, _ = run_pipeline(letterbox_video, tmp_path, cfg, force=["all"])
    assert statuses(rec) == {"ingest": "forced", "asr": "forced", "visual": "forced", "ocr": "forced", "report": "forced"}
    rec, _ = run_pipeline(letterbox_video, tmp_path, cfg)
    assert set(statuses(rec).values()) == {"hit"}


def test_missing_output_file_is_a_miss(letterbox_video, srt, tmp_path):
    cfg = Config(transcript=srt, layout_samples=4)
    _, run_dir = run_pipeline(letterbox_video, tmp_path, cfg)
    (run_dir / "work/audio.wav").unlink()
    rec, _ = run_pipeline(letterbox_video, tmp_path, cfg)
    assert statuses(rec) == {"ingest": "miss", "asr": "hit", "visual": "hit", "ocr": "hit", "report": "hit"}  # same inputs, same key: downstream stays valid
    assert (run_dir / "work/audio.wav").is_file()


def test_unknown_force_stage_is_an_error(letterbox_video, srt, tmp_path):
    with pytest.raises(StageError, match="unknown stage"):
        run_pipeline(letterbox_video, tmp_path, Config(transcript=srt), force=["no-such-stage"])


class TestPlan:
    def test_auto_skips_unresolvable_whisperkit_and_records_why(self):
        plan = build_plan(Config(asr="auto"))
        assert [s.name for s in plan.stages] == ["ingest", "asr.parakeet", "asr", "visual", "ocr", "report"]
        assert [(s.stage, s.status) for s in plan.skipped] == [("asr.whisperkit", "skipped")]
        assert "no CLI configured" in plan.skipped[0].reason

    def test_auto_includes_whisperkit_when_it_resolves(self, tmp_path):
        cli = tmp_path / "cli"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        plan = build_plan(Config(asr="auto", whisperkit_cli=str(cli), whisperkit_model=str(tmp_path)))
        assert [s.name for s in plan.stages] == ["ingest", "asr.parakeet", "asr.whisperkit", "asr", "visual", "ocr", "report"]
        assert plan.skipped == []

    @pytest.mark.parametrize("mode", ["both", "whisperkit"])
    def test_explicit_whisperkit_that_does_not_resolve_is_an_error(self, mode):
        with pytest.raises(StageError, match="needs WhisperKit"):
            build_plan(Config(asr=mode))

    def test_whisperkit_only_has_no_parakeet(self, tmp_path):
        cli = tmp_path / "cli"
        cli.write_text("")
        cli.chmod(0o755)
        plan = build_plan(Config(asr="whisperkit", whisperkit_cli=str(cli), whisperkit_model=str(tmp_path)))
        assert [s.name for s in plan.stages] == ["ingest", "asr.whisperkit", "asr", "visual", "ocr", "report"]

    def test_transcript_conflicts_with_explicit_backend(self, tmp_path):
        f = tmp_path / "t.srt"
        f.write_text("")
        with pytest.raises(StageError, match="replaces ASR"):
            build_plan(Config(asr="parakeet", transcript=f))


def test_cli_run_with_transcript_and_errors(letterbox_video, srt, tmp_path):
    runner = CliRunner()
    out = tmp_path / "o"
    res = runner.invoke(app, ["run", str(letterbox_video), "--out", str(out), "--transcript", str(srt), "--content-rect", "0,102,960,516"])
    assert res.exit_code == 0, res.output
    assert (out / "letterbox/work/transcript.json").is_file()
    res = runner.invoke(app, ["run", str(letterbox_video), "--out", str(out), "--transcript", str(srt), "--content-rect", "0,102,960,516", "--force", "asr"])
    assert res.exit_code == 0 and "ingest" in res.output and "cached" in res.output and "done" in res.output
    bad = runner.invoke(app, ["run", str(letterbox_video), "--out", str(out), "--transcript", str(srt), "--force", "no-such-stage"])
    assert bad.exit_code == 2 and "unknown stage" in bad.output
    bad = runner.invoke(app, ["run", str(letterbox_video), "--out", str(out), "--asr", "both"])
    assert bad.exit_code == 2 and "needs WhisperKit" in bad.output
    bad = runner.invoke(app, ["run", str(letterbox_video), "--out", str(out), "--content-rect", "1,2,3"])
    assert bad.exit_code != 0


def interrupt_first_progress(monkeypatch):
    real = ingest.extract_audio_wav

    def interrupted(source, dest, on_progress=lambda s: None):
        def stop(seconds):
            on_progress(seconds)
            raise KeyboardInterrupt

        real(source, dest, stop)

    monkeypatch.setattr(ingest, "extract_audio_wav", interrupted)


def test_interrupted_ingest_never_reads_as_a_cache_hit(letterbox_video, srt, tmp_path, monkeypatch):
    cfg = Config(transcript=srt, layout_samples=4)
    _, run_dir = run_pipeline(letterbox_video, tmp_path, cfg)
    wav = run_dir / "work/audio.wav"
    good = wav.read_bytes()
    interrupt_first_progress(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(letterbox_video, tmp_path, cfg, force=["ingest"])
    # the half-written file never replaced the good one, and the old key is gone
    assert wav.read_bytes() == good and not list((run_dir / "work").glob("*.part"))
    assert not (run_dir / "work/ingest.json.key").exists()
    monkeypatch.undo()
    rec, _ = run_pipeline(letterbox_video, tmp_path, cfg)
    assert statuses(rec)["ingest"] == "miss" and wav.read_bytes() == good  # before: "hit" on a truncated wav


def test_interrupted_first_ingest_leaves_no_audio_and_no_key(letterbox_video, srt, tmp_path, monkeypatch):
    interrupt_first_progress(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(letterbox_video, tmp_path, Config(transcript=srt, layout_samples=4))
    work = tmp_path / "letterbox/work"
    assert not (work / "audio.wav").exists() and not list(work.glob("*.part")) and not (work / "ingest.json.key").exists()


def test_truncated_audio_side_file_is_a_miss(letterbox_video, srt, tmp_path):
    cfg = Config(transcript=srt, layout_samples=4)
    _, run_dir = run_pipeline(letterbox_video, tmp_path, cfg)
    wav = run_dir / "work/audio.wav"
    good = wav.read_bytes()
    wav.write_bytes(good[: len(good) // 3])
    rec, _ = run_pipeline(letterbox_video, tmp_path, cfg)
    assert statuses(rec)["ingest"] == "miss" and wav.read_bytes() == good


def test_video_without_audio_still_works_with_a_transcript(noaudio_video, srt, tmp_path):
    rec, run_dir = run_pipeline(noaudio_video, tmp_path, Config(transcript=srt, layout_samples=4))
    art = IngestArtifact.model_validate_json((run_dir / "work/ingest.json").read_text())
    assert art.audio is None and art.audio_wav is None
    assert statuses(rec) == {"ingest": "miss", "asr": "miss", "visual": "miss", "ocr": "miss", "report": "miss"}
    again, _ = run_pipeline(noaudio_video, tmp_path, Config(transcript=srt, layout_samples=4))
    assert set(statuses(again).values()) == {"hit"}


@pytest.mark.parametrize("mode", ["parakeet", "whisperkit"])
def test_asr_on_a_video_without_audio_fails_loudly(noaudio_video, tmp_path, mode):
    events = []
    cfg = Config(asr=mode, layout_samples=4)
    if mode == "whisperkit":
        cli = tmp_path / "cli"
        cli.write_text("#!/bin/sh\n")
        cli.chmod(0o755)
        cfg = replace(cfg, whisperkit_cli=str(cli), whisperkit_model=str(tmp_path))
    with pytest.raises(StageError, match="no audio stream"):
        run_pipeline(noaudio_video, tmp_path / "o", cfg, sink=events.append)
    assert events[-1]["event"] == "run_end" and events[-1]["status"] == "failed"
    # a host tells this failure from others by its kind, not by the wording
    failed = next(e for e in events if e["event"] == "stage_end" and e["status"] == "failed")
    assert failed["error_kind"] == events[-1]["error_kind"] == "no_audio"


def test_unreadable_video_gives_events_and_a_clean_error_not_a_traceback(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"this is not a video" * 100)
    events = []
    with pytest.raises(StageError, match="ffprobe failed"):
        run_pipeline(junk, tmp_path / "o", Config(), sink=events.append)
    assert [e["event"] for e in events] == ["run_start", "run_end"] and events[-1]["status"] == "failed"
    res = CliRunner().invoke(app, ["run", str(junk), "--out", str(tmp_path / "o"), "--progress", "json"])
    assert res.exit_code == 2 and "error:" in res.output
    assert [json.loads(line)["event"] for line in res.stdout.splitlines()] == ["run_start", "run_end"]


def test_captions_after_the_end_of_the_video_are_dropped_and_the_last_is_cut_off():
    from framescribe.models import Caption

    caps = [Caption(start=1, end=2, text="in"), Caption(start=9, end=13, text="cut"), Caption(start=10.0, end=12, text="gone"),
            Caption(start=14, end=16, text="gone")]
    kept = ingest.within_duration(caps, 10.0)
    assert [(c.text, c.end) for c in kept] == [("in", 2), ("cut", 10.0)]


def test_failed_run_still_writes_run_json_with_the_failed_stage(letterbox_video, tmp_path, monkeypatch):
    from framescribe.stages import asr_parakeet

    def boom(ctx):
        raise StageError("model exploded")

    monkeypatch.setattr(asr_parakeet, "STAGE", replace(asr_parakeet.STAGE, run=boom))
    with pytest.raises(StageError):
        run_pipeline(letterbox_video, tmp_path, Config(layout_samples=4))
    rec = json.loads((tmp_path / "letterbox/run.json").read_text())
    assert rec["status"] == "failed" and rec["error"] == "model exploded"
    assert [(s["stage"], s["status"]) for s in rec["stages"]][:2] == [("ingest", "miss"), ("asr.parakeet", "failed")]
    assert rec["stages"][-1]["status"] == "skipped"


def test_interrupted_run_writes_run_json_marked_interrupted(letterbox_video, tmp_path, monkeypatch):
    from framescribe.stages import asr_parakeet

    def stop(ctx):
        raise KeyboardInterrupt

    monkeypatch.setattr(asr_parakeet, "STAGE", replace(asr_parakeet.STAGE, run=stop))
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(letterbox_video, tmp_path, Config(layout_samples=4))
    assert json.loads((tmp_path / "letterbox/run.json").read_text())["status"] == "interrupted"


def test_black_video_gets_a_full_frame_layout_and_a_logged_warning(black_video, srt, tmp_path):
    events = []
    _, run_dir = run_pipeline(black_video, tmp_path, Config(transcript=srt, layout_samples=4), sink=events.append)
    art = IngestArtifact.model_validate_json((run_dir / "work/ingest.json").read_text())
    assert (art.layout.content_rect.w, art.layout.content_rect.h) == (640, 360) and "fallback" in art.layout.method
    assert any(e["event"] == "log" and e["stage"] == "ingest" and "not confident" in e["message"] for e in events)
    assert "not confident" in (run_dir / "work/ingest.log").read_text()


def test_a_failed_completeness_gate_leaves_its_figures_and_gaps_and_is_not_cached(letterbox_video, tmp_path, monkeypatch):
    from framescribe.models import AsrArtifact, AsrPass, Word
    from framescribe.pipeline import asr_parakeet
    from framescribe.words import segment_sentences

    def ws(text, start):
        return [Word(text=t, start=round(start + 0.1 * i, 2), end=round(start + 0.1 * i + 0.08, 2))
                for i, t in enumerate(text.split())]

    primary = ws("so we load it", 0.1) + ws("and it runs", 2.5)
    heard = primary[:4] + ws("then we click print here", 1.0) + primary[4:]  # the second pass has more

    def fake(ctx):
        return AsrArtifact(backend="parakeet", model="m", language=None, words=primary,
                           segments=segment_sentences(primary), audio_seconds=3.0, wall_seconds=0.1,
                           second_pass=AsrPass(name="parakeet-pass2", words=heard, cuts=[]))

    monkeypatch.setattr(asr_parakeet, "STAGE", replace(asr_parakeet.STAGE, run=fake))
    out = tmp_path / "out"
    cfg = Config(asr="parakeet", layout_samples=4, strict_completeness=True)  # by default the result is kept, warned
    for _ in range(2):  # the second run computes the stage again: a refused transcript is never a cache hit
        with pytest.raises(StageError, match=r"gaps in work/transcript\.gate-failed\.json") as failure:
            run_pipeline(letterbox_video, out, cfg)
        run_dir = out / "letterbox"
        record = json.loads((run_dir / "run.json").read_text())
        assert record["status"] == "failed" and (record["stages"][-1]["stage"], record["stages"][-1]["status"]) == ("asr", "failed")
        assert record["completeness"]["filled_words"] == 5 and record["completeness"]["filled_share"] > 0.03
        refused = TranscriptArtifact.model_validate_json((run_dir / "work/transcript.gate-failed.json").read_text())
        assert refused.completeness.filled_words == 5 and refused.completeness.gaps[0].text == "then we click print here"
        assert "5 of the transcript's words" in str(failure.value)
        assert not (run_dir / "work/transcript.json").exists()
    # without --strict-completeness the same run simply succeeds: kept, with the warning, from the cached speech
    # recognition, and the diagnostics file is gone
    rec, run_dir = run_pipeline(letterbox_video, out, replace(cfg, strict_completeness=False))
    assert statuses(rec)["asr.parakeet"] == "hit" and statuses(rec)["asr"] == "miss" and rec.status == "ok"
    assert not (run_dir / "work/transcript.gate-failed.json").exists()
    figures = json.loads((run_dir / "run.json").read_text())["completeness"]
    assert figures["above_limit"] and figures["limit"] == 0.03 and figures["warning"].startswith("5 of the transcript's")


def test_an_imported_transcript_is_said_to_be_taken_as_given(letterbox_video, srt, tmp_path):
    from framescribe.stages import asr

    _, run_dir = run_pipeline(letterbox_video, tmp_path, Config(transcript=srt, layout_samples=4))
    t = TranscriptArtifact.model_validate_json((run_dir / "work/transcript.json").read_text())
    assert t.completeness is None and "taken as given: not cross-checked" in asr.describe(t)
    assert "taken as given: not cross-checked" in (run_dir / "transcript.md").read_text()
    assert "| Completeness | imported transcript, taken as given: not cross-checked |" in (run_dir / "report.md").read_text()


def test_an_overridden_completeness_limit_and_the_speech_warnings_are_said_in_the_report_and_transcript(
        letterbox_video, tmp_path, monkeypatch):
    from framescribe.models import AsrArtifact, AsrPass, Word
    from framescribe.pipeline import asr_parakeet
    from framescribe.words import segment_sentences

    def ws(text, start):
        return [Word(text=t, start=round(start + 0.1 * i, 2), end=round(start + 0.1 * i + 0.08, 2))
                for i, t in enumerate(text.split())]

    primary = ws("so we load it", 0.1) + ws("and it runs", 2.5)
    heard = primary[:4] + ws("then we click print here", 1.0) + primary[4:]
    warnings = ["chunk 1.00-2.00 s: 4 words without punctuation kept (the decode with the most words of 2)",
                "pass 2: chunk 1.00-2.00 s: 4 words without punctuation kept (the decode with the most words of 2)",
                "chunk 2.00-3.00 s: no words, although 1.0 s of it is above the pause threshold"]

    def fake(ctx):
        return AsrArtifact(backend="parakeet", model="m", language=None, words=primary,
                           segments=segment_sentences(primary), audio_seconds=3.0, wall_seconds=0.1, warnings=warnings,
                           second_pass=AsrPass(name="parakeet-pass2", words=heard, cuts=[]))

    monkeypatch.setattr(asr_parakeet, "STAGE", replace(asr_parakeet.STAGE, run=fake))
    cfg = Config(asr="parakeet", layout_samples=4)
    _, run_dir = run_pipeline(letterbox_video, tmp_path / "out", cfg)
    report = (run_dir / "report.md").read_text()
    row = next(x for x in report.split("\n") if x.startswith("| Completeness |"))
    assert "| **ABOVE THE USUAL LEVEL: 5 of the transcript's 12 words \\(41.7%\\) were filled in" in row
    assert "more than the usual 3.0%" in row and "check them against the recording.**" in row
    assert "| Speech recognition warnings | 2 chunks kept without punctuation, 1 chunk that stayed empty" in report
    said = next(x for x in report.split("\n") if x.startswith("- SAID lines"))
    assert "from a second decode of the audio, the only other source" in said and "another speech recognition" not in said
    transcript_md = (run_dir / "transcript.md").read_text()
    top = transcript_md.split("**[")[0]  # before the first segment
    assert "**Completeness: ABOVE THE USUAL LEVEL: 5 of the transcript's 12 words \\(41.7%\\)" in top
    # within the limit nothing of this is said
    quiet = replace(cfg, max_filled_share=0.5)
    _, run_dir = run_pipeline(letterbox_video, tmp_path / "out", quiet)
    assert "ABOVE THE USUAL" not in (run_dir / "report.md").read_text() + (run_dir / "transcript.md").read_text()


def test_a_stage_s_metrics_go_to_run_json_and_the_cache_key_ignores_the_memory_bound(letterbox_video, tmp_path, monkeypatch):
    from framescribe.models import AsrArtifact, AsrPass, Word
    from framescribe.pipeline import asr_parakeet
    from framescribe.words import segment_sentences

    words = [Word(text=t, start=round(0.1 + 0.1 * i, 2), end=round(0.1 + 0.1 * i + 0.08, 2))
             for i, t in enumerate("so we load it and it runs".split())]

    def fake(ctx):
        ctx.metrics.update(mlx_peak_mb=1234.5, mlx_cache_limit_mb=ctx.config.mlx_cache_limit_mb)
        return AsrArtifact(backend="parakeet", model="m", language=None, words=words, segments=segment_sentences(words),
                           audio_seconds=3.0, wall_seconds=0.1, second_pass=AsrPass(name="parakeet-pass2", words=words, cuts=[]))

    monkeypatch.setattr(asr_parakeet, "STAGE", replace(asr_parakeet.STAGE, run=fake))
    assert "mlx_cache_limit_mb" not in asr_parakeet.STAGE.config_fields
    out = tmp_path / "out"
    cfg = Config(asr="parakeet", layout_samples=4)
    rec, run_dir = run_pipeline(letterbox_video, out, cfg)
    stages = {s["stage"]: s for s in json.loads((run_dir / "run.json").read_text())["stages"]}
    assert stages["asr.parakeet"]["status"] == "miss"
    assert stages["asr.parakeet"]["metrics"] == {"mlx_peak_mb": 1234.5, "mlx_cache_limit_mb": 512}
    assert stages["ingest"]["metrics"] == {}
    # another memory bound is still a cache hit, and a hit carries no metrics of its own
    rec, run_dir = run_pipeline(letterbox_video, out, replace(cfg, mlx_cache_limit_mb=64))
    assert statuses(rec)["asr.parakeet"] == "hit"
    stages = {s["stage"]: s for s in json.loads((run_dir / "run.json").read_text())["stages"]}
    assert stages["asr.parakeet"]["metrics"] == {}
