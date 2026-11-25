"""Event stream, renderers, and child-process cleanup."""

import io
import json
import os
import stat
import subprocess
import time
import wave
from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from framescribe.cli import app
from framescribe.config import Config
from framescribe.models import (
    AsrArtifact,
    AudioFacts,
    IngestArtifact,
    Layout,
    Rect,
    TranscriptArtifact,
    VideoFacts,
    Word,
)
from framescribe.pipeline import run_pipeline
from framescribe.proc import child_process
from framescribe.stages import asr_parakeet, asr_whisperkit
from framescribe.stages.base import Context, StageError
from framescribe.status import PlainRenderer, RichRenderer

FIXTURES = Path(__file__).parent / "fixtures"


def kinds(events, stage=None):
    return [(e["event"], e.get("status")) for e in events if stage is None or e.get("stage") == stage]


def fake_asr(run):
    return replace(asr_parakeet.STAGE, run=run)


def ok_asr(ctx):
    ctx.log("loading the fake model")
    for i in range(3):
        ctx.progress(i, 3, "chunks", f"chunk {i + 1}/3")
    return AsrArtifact(backend="parakeet", model="fake", language=None, words=[Word(text="Hi.", start=0, end=0.5)],
                       segments=[], audio_seconds=3.0, wall_seconds=1.0)


@pytest.fixture
def cfg():
    return Config(layout_samples=4)


def test_event_sequence_done_then_cached_with_skipped_backend(letterbox_video, tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(asr_parakeet, "STAGE", fake_asr(ok_asr))
    monkeypatch.setattr("framescribe.pipeline.asr_parakeet", asr_parakeet)
    first, second = [], []
    run_pipeline(letterbox_video, tmp_path, cfg, sink=first.append)
    run_pipeline(letterbox_video, tmp_path, cfg, sink=second.append)

    assert first[0]["event"] == "run_start" and first[-1]["event"] == "run_end" and first[-1]["status"] == "ok"
    assert [s["name"] for s in first[0]["stages"]] == ["ingest", "asr.parakeet", "asr.whisperkit", "asr", "visual", "ocr", "report"]
    assert kinds(first, "asr.whisperkit") == [("stage_end", "skipped")]
    assert "no CLI configured" in next(e for e in first if e.get("stage") == "asr.whisperkit")["reason"]
    assert kinds(first, "asr.parakeet")[0] == ("stage_start", None) and kinds(first, "asr.parakeet")[-1] == ("stage_end", "done")
    progress = [e for e in first if e["event"] == "stage_progress" and e["stage"] == "asr.parakeet"]
    assert [(p["done"], p["total"], p["unit"], p["note"]) for p in progress] == [(i, 3, "chunks", f"chunk {i + 1}/3") for i in range(3)]
    assert any(e["event"] == "log" and e["message"] == "loading the fake model" for e in first)
    assert any(e["event"] == "stage_progress" and e["stage"] == "ingest" and e["unit"] == "audio_s" for e in first)
    assert [e["t"] for e in first] == sorted(e["t"] for e in first)

    ended = {e["stage"]: e["status"] for e in second if e["event"] == "stage_end"}
    assert ended == {"ingest": "cached", "asr.parakeet": "cached", "asr.whisperkit": "skipped", "asr": "cached", "visual": "cached", "ocr": "cached", "report": "cached"}
    assert not [e for e in second if e["event"] == "stage_progress"]


def test_failed_stage_reports_stage_message_and_log_path(letterbox_video, tmp_path, monkeypatch, cfg):
    def boom(ctx):
        ctx.log("about to fail")
        raise StageError("model exploded")

    monkeypatch.setattr(asr_parakeet, "STAGE", fake_asr(boom))
    events = []
    with pytest.raises(StageError):
        run_pipeline(letterbox_video, tmp_path, cfg, sink=events.append)
    end = next(e for e in events if e["event"] == "stage_end" and e["stage"] == "asr.parakeet")
    assert end["status"] == "failed" and end["error"] == "model exploded"
    assert end["log_path"].endswith("asr.parakeet.log") and Path(end["log_path"]).read_text() == "about to fail\n"
    assert events[-1]["event"] == "run_end" and events[-1]["status"] == "failed"


def test_interrupt_ends_run_cleanly_and_keeps_finished_stages_cached(letterbox_video, tmp_path, monkeypatch, cfg):
    def interrupted(ctx):
        raise KeyboardInterrupt

    monkeypatch.setattr(asr_parakeet, "STAGE", fake_asr(interrupted))
    events = []
    with pytest.raises(KeyboardInterrupt):
        run_pipeline(letterbox_video, tmp_path, cfg, sink=events.append)
    assert events[-1]["status"] == "interrupted"
    assert next(e for e in events if e["event"] == "stage_end" and e["stage"] == "asr.parakeet")["error"] == "interrupted"

    monkeypatch.setattr(asr_parakeet, "STAGE", fake_asr(ok_asr))
    again = []
    run_pipeline(letterbox_video, tmp_path, cfg, sink=again.append)
    assert next(e for e in again if e["event"] == "stage_end" and e["stage"] == "ingest")["status"] == "cached"


def test_json_mode_writes_only_json_lines_to_stdout(letterbox_video, tmp_path):
    srt = tmp_path / "t.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello.\n")
    res = CliRunner().invoke(app, ["run", str(letterbox_video), "--out", str(tmp_path / "o"), "--transcript", str(srt),
                                   "--progress", "json"])
    assert res.exit_code == 0, res.output
    lines = res.stdout.splitlines()
    events = [json.loads(line) for line in lines]  # every line is valid JSON
    assert events[0]["event"] == "run_start" and events[-1]["event"] == "run_end"
    assert {e["event"] for e in events} >= {"stage_start", "stage_progress", "stage_end"}


def test_plain_renderer_throttles_progress_lines():
    out = io.StringIO()
    render = PlainRenderer(out)
    render({"event": "stage_start", "t": 0.0, "stage": "asr.parakeet"})
    for t in (0.0, 1.0, 4.9, 5.0, 9.9, 10.0, 10.5):
        render({"event": "stage_progress", "t": t, "stage": "asr.parakeet", "done": t, "total": 20.0, "unit": "audio_s",
                "note": "chunk"})
    render({"event": "stage_end", "t": 11.0, "stage": "asr.parakeet", "status": "done", "wall_seconds": 11.0, "summary": "ok"})
    lines = out.getvalue().splitlines()
    assert [ln[:10] for ln in lines] == ["[00:00:00]", "[00:00:00]", "[00:00:05]", "[00:00:10]", "[00:00:11]"]
    assert "25% 00:00:05 / 00:00:20 chunk" in lines[2]


def test_rich_panel_shows_running_stage_details_and_final_summary():
    clock = [100.0]
    console = Console(file=io.StringIO(), force_terminal=False, width=120, record=True)
    r = RichRenderer(console, now=lambda: clock[0])
    r({"event": "run_start", "t": 0, "source": "/x/talk.mp4", "duration": 6769.0, "run_dir": "/x/out/talk",
       "stages": [{"name": "ingest", "skipped_reason": None}, {"name": "asr.parakeet", "skipped_reason": None},
                  {"name": "asr.whisperkit", "skipped_reason": "no CLI configured"}]})
    r({"event": "stage_start", "t": 0, "stage": "ingest"})
    r({"event": "stage_end", "t": 1, "stage": "ingest", "status": "done", "wall_seconds": 13.3, "summary": "layout ok"})
    r({"event": "stage_end", "t": 1, "stage": "asr.whisperkit", "status": "skipped", "wall_seconds": 0, "reason": "no CLI configured"})
    r({"event": "stage_start", "t": 1, "stage": "asr.parakeet"})
    clock[0] = 140.0  # 40 s into the stage
    r({"event": "stage_progress", "t": 41, "stage": "asr.parakeet", "done": 3320.0, "total": 6640.0, "unit": "audio_s",
       "note": "chunk 14/57"})
    console.print(r.panel())
    text = console.export_text()
    for expected in ("done", "skipped", "no CLI configured", "running", "50%", "00:55:20 / 01:50:40", "ETA 00:00:40",
                     "83x realtime", "chunk 14/57", "stages 2/3", "talk.mp4", "01:52:49"):
        assert expected in text, expected
    r({"event": "stage_end", "t": 80, "stage": "asr.parakeet", "status": "done", "wall_seconds": 79.0, "summary": "1 words"})
    r({"event": "run_end", "t": 80, "status": "ok", "run_dir": "/x/out/talk", "wall_seconds": 80.0,
       "outputs": ["/x/out/talk/transcript.md"]})
    final = console.export_text()
    assert "framescribe run" in final and "1 words" in final and "/x/out/talk/transcript.md" in final


def test_rich_renderer_survives_failure():
    console = Console(file=io.StringIO(), force_terminal=False, width=100, record=True)
    r = RichRenderer(console)
    r({"event": "run_start", "t": 0, "source": "/x/a.mp4", "duration": 10.0, "run_dir": "/x/o",
       "stages": [{"name": "ingest", "skipped_reason": None}]})
    r({"event": "stage_start", "t": 0, "stage": "ingest"})
    r({"event": "stage_end", "t": 1, "stage": "ingest", "status": "failed", "wall_seconds": 1.0, "error": "bad", "log_path": "/x/o/ingest.log"})
    r({"event": "run_end", "t": 1, "status": "failed", "run_dir": "/x/o", "wall_seconds": 1.0, "error": "bad"})
    out = console.export_text()
    assert "failed" in out and "/x/o/ingest.log" in out and "bad" in out


def test_child_process_is_killed_when_the_parent_is_interrupted():
    with pytest.raises(KeyboardInterrupt):
        with child_process(["sleep", "60"]) as proc:
            raise KeyboardInterrupt
    assert proc.poll() is not None


def stage_context(tmp_path, emit):
    wav = tmp_path / "work" / "audio.wav"
    wav.parent.mkdir(parents=True)
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(2 * 16000 * 100))  # 100 s of silence
    ingest = IngestArtifact(source="x", duration=100.0, video=VideoFacts(width=1, height=1, fps=1, codec="h264"),
                            audio=AudioFacts(codec="aac", sample_rate=16000, channels=1), audio_wav="work/audio.wav",
                            embedded_captions=[], layout=Layout(frame_w=1, frame_h=1, content_rect=Rect(x=0, y=0, w=1, h=1),
                                                                method="m", confidence=1))
    model = tmp_path / "model"
    model.mkdir()
    return Context(config=Config(), source=tmp_path / "v.mp4", run_dir=tmp_path, artifacts={"ingest": ingest}, emit=emit,
                   stage="asr.whisperkit"), model


def fake_cli(tmp_path, body):
    script = tmp_path / "fake-whisperkit"
    script.write_text("#!/bin/bash\n"
                      'while [ $# -gt 0 ]; do case "$1" in --report-path) RP="$2";; esac; shift; done\n'
                      f'echo $$ > "{tmp_path}/pid"\n' + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def wait_for_pid(path: Path) -> int:
    """The fake CLI's pid, once it has written it (it may not have when the stage reports its first progress)."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        text = path.read_text().strip() if path.is_file() else ""
        if text:
            return int(text)
        time.sleep(0.05)
    raise AssertionError(f"{path} was not written")


def test_whisperkit_stage_parses_progress_bar_and_report(tmp_path):
    report = FIXTURES / "whisperkit_report.json"
    cli = fake_cli(tmp_path, "printf '[###     ] 40%% | Elapsed Time: 1.0 s\\r'\nsleep 1.5\n"
                             f'cp "{report}" "$RP/audio.json"\n')
    events = []
    ctx, model = stage_context(tmp_path, lambda event, **f: events.append((event, f)))
    ctx.config = Config(whisperkit_cli=str(cli), whisperkit_model=str(model))
    artifact = asr_whisperkit.run(ctx)
    assert artifact.backend == "whisperkit" and artifact.words
    notes = [f["note"] for e, f in events if e == "stage_progress"]
    assert any(n == "WhisperKit 00:00:40 / 00:01:40" for n in notes), notes  # 40% of 100 s


def test_whisperkit_stage_estimates_when_the_cli_prints_no_progress(tmp_path):
    cli = fake_cli(tmp_path, f'sleep 2.5\ncp "{FIXTURES / "whisperkit_report.json"}" "$RP/audio.json"\n')
    events = []
    ctx, model = stage_context(tmp_path, lambda event, **f: events.append((event, f)))
    ctx.config = Config(whisperkit_cli=str(cli), whisperkit_model=str(model))
    asr_whisperkit.run(ctx)
    assert any("estimate only" in (f["note"] or "") for e, f in events if e == "stage_progress")


def test_interrupt_kills_the_whisperkit_process(tmp_path):
    cli = fake_cli(tmp_path, "printf '[#      ] 10%% | Elapsed Time: 1.0 s\\r'\nsleep 60\n")

    def emit(event, **fields):
        if event == "stage_progress":
            raise KeyboardInterrupt

    ctx, model = stage_context(tmp_path, emit)
    ctx.config = Config(whisperkit_cli=str(cli), whisperkit_model=str(model))
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        asr_whisperkit.run(ctx)
    assert time.monotonic() - t0 < 20
    pid = int((tmp_path / "pid").read_text())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_rich_summary_keeps_the_whole_log_path_at_80_columns():
    long_log = "/very/long/output/directory/for/a/sample clip (copy 2)/work/asr.whisperkit.log"
    console = Console(file=io.StringIO(), force_terminal=False, width=80, record=True)
    r = RichRenderer(console)
    r({"event": "run_start", "t": 0, "source": "/x/a.mp4", "duration": 10.0, "run_dir": "/x/o",
       "stages": [{"name": "asr.whisperkit", "skipped_reason": None}]})
    r({"event": "stage_start", "t": 0, "stage": "asr.whisperkit"})
    r({"event": "stage_end", "t": 1, "stage": "asr.whisperkit", "status": "failed", "wall_seconds": 1.0, "error": "exit 3",
       "log_path": long_log})
    r({"event": "run_end", "t": 1, "status": "failed", "run_dir": "/x/o", "wall_seconds": 1.0, "error": "exit 3"})
    assert long_log in console.export_text()  # contiguous: not truncated and not broken by table borders


@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_termination_signal_stops_the_run_cleanly_and_kills_whisperkit(letterbox_video, tmp_path, sig):
    # SIGHUP used to end the process at once: no run_end event, run.json not updated, whisperkit-cli orphaned
    import json as _json
    import signal
    import sys

    cli = fake_cli(tmp_path, "printf '[#      ] 10%% | Elapsed Time: 1.0 s\\r'\nsleep 120\n")
    model = tmp_path / "model"
    model.mkdir()
    exe = Path(sys.executable).parent / "framescribe"
    proc = subprocess.Popen(
        [str(exe), "run", str(letterbox_video), "--out", str(tmp_path / "o"), "--asr", "whisperkit", "--progress", "json",
         "--whisperkit-cli", str(cli), "--whisperkit-model", str(model)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    lines = []
    for line in proc.stdout:
        lines.append(_json.loads(line))
        if lines[-1]["event"] == "stage_progress" and lines[-1]["stage"] == "asr.whisperkit":
            break
    pid = wait_for_pid(tmp_path / "pid")
    signum = getattr(signal, sig)
    proc.send_signal(signum)
    rest = [_json.loads(line) for line in proc.stdout]
    code = proc.wait(timeout=30)
    assert code == 128 + signum
    assert rest[-1]["event"] == "run_end" and rest[-1]["status"] == "interrupted"
    assert any(e["event"] == "stage_end" and e["stage"] == "asr.whisperkit" and e["status"] == "failed" for e in rest)
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert _json.loads((tmp_path / "o/letterbox/run.json").read_text())["status"] == "interrupted"


def test_interrupted_forced_backend_rerun_does_not_leave_the_transcript_stale(letterbox_video, tmp_path):
    # The backend reruns with a new result under the same cache key; before, `asr` then still read as a hit and
    # transcript.json kept the old text.
    word, delay = tmp_path / "word.txt", tmp_path / "delay.txt"
    report = ('{"language":"en","segments":[{"words":[{"word":" \'"$(cat ' + str(word) + ')"\'.","start":1.0,"end":1.5,'
              '"probability":0.9}]}]}')
    cli = fake_cli(tmp_path, f'sleep "$(cat {delay})"\nprintf \'%s\' \'{report}\' > "$RP/audio.json"\n')
    model = tmp_path / "model"
    model.mkdir()
    cfg = Config(asr="whisperkit", layout_samples=4, whisperkit_cli=str(cli), whisperkit_model=str(model))
    out = tmp_path / "o"

    def text(run_dir):
        return [s.text for s in TranscriptArtifact.model_validate_json((run_dir / "work/transcript.json").read_text()).segments]

    word.write_text("alpha")
    delay.write_text("0")
    _, run_dir = run_pipeline(letterbox_video, out, cfg)
    assert text(run_dir) == ["alpha."]

    word.write_text("beta")
    delay.write_text("30")

    def interrupt(event):
        if event["event"] == "stage_progress" and event["stage"] == "asr.whisperkit":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_pipeline(letterbox_video, out, cfg, force=["asr.whisperkit"], sink=interrupt)
    delay.write_text("0")
    rec, _ = run_pipeline(letterbox_video, out, cfg)
    assert {s.stage: s.status for s in rec.stages} == {
        "ingest": "hit", "asr.whisperkit": "miss", "asr": "miss", "visual": "hit", "ocr": "hit", "report": "miss"}
    assert text(run_dir) == ["beta."]


def test_whisperkit_reparses_its_retained_report_when_only_the_parsing_changed(letterbox_video, tmp_path, monkeypatch):
    calls, report = tmp_path / "calls", tmp_path / "report.json"
    report.write_text('{"language":"en","segments":[{"seek":0,"words":[{"word":" Hello.","start":1.0,"end":1.5}]}]}')
    cli = fake_cli(tmp_path, f'echo run >> "{calls}"\ncp "{report}" "$RP/audio.json"\n')
    model = tmp_path / "model"
    model.mkdir()
    cfg = Config(asr="whisperkit", layout_samples=4, whisperkit_cli=str(cli), whisperkit_model=str(model))
    out = tmp_path / "o"
    _, run_dir = run_pipeline(letterbox_video, out, cfg)
    first = AsrArtifact.model_validate_json((run_dir / "work/asr.whisperkit.json").read_text())
    assert calls.read_text().count("run") == 1

    from framescribe import pipeline

    monkeypatch.setattr(pipeline.asr_whisperkit, "STAGE", replace(asr_whisperkit.STAGE, version=asr_whisperkit.VERSION + 1))
    events = []
    rec, _ = run_pipeline(letterbox_video, out, cfg, sink=events.append)
    assert {s.stage: s.status for s in rec.stages}["asr.whisperkit"] == "miss"
    assert calls.read_text().count("run") == 1  # parsed again, not transcribed again
    assert AsrArtifact.model_validate_json((run_dir / "work/asr.whisperkit.json").read_text()) == first
    assert any(e["event"] == "log" and "re-parsing" in e["message"] for e in events)

    run_pipeline(letterbox_video, out, cfg, force=["asr.whisperkit"])
    assert calls.read_text().count("run") == 2  # --force runs the CLI
    other = replace(cfg, language="de")  # different options: the retained report does not apply
    run_pipeline(letterbox_video, out, other)
    assert calls.read_text().count("run") == 3
