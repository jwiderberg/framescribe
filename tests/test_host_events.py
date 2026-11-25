"""What a host application reads from the event streams: `run_end`'s completeness figures and warnings, `setup
--progress json`, and `python -m framescribe`. No network, no model."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from framescribe import __version__, doctor, hub
from framescribe.cli import app
from framescribe.config import Config
from framescribe.models import AsrArtifact, AsrPass, Word
from framescribe.pipeline import run_pipeline
from framescribe.stages import asr_parakeet
from framescribe.stages.base import StageError
from framescribe.words import segment_sentences

runner = CliRunner()


def ws(text, start):
    return [Word(text=t, start=round(start + 0.1 * i, 2), end=round(start + 0.1 * i + 0.08, 2))
            for i, t in enumerate(text.split())]


PRIMARY = ws("so we load it", 0.1) + ws("and it runs", 2.5)
HEARD = PRIMARY[:4] + ws("then we click print here", 1.0) + PRIMARY[4:]  # the second pass has five words more
SPEECH_WARNING = "chunk 1.00-2.00 s: 4 words without punctuation kept (the decode with the most words of 2)"


@pytest.fixture
def fake_parakeet(monkeypatch):
    def fake(ctx):
        return AsrArtifact(backend="parakeet", model="m", language=None, words=PRIMARY,
                           segments=segment_sentences(PRIMARY), audio_seconds=3.0, wall_seconds=0.1,
                           warnings=[SPEECH_WARNING], second_pass=AsrPass(name="parakeet-pass2", words=HEARD, cuts=[]))

    monkeypatch.setattr(asr_parakeet, "STAGE", replace(asr_parakeet.STAGE, run=fake))


# ---- run_end --------------------------------------------------------------------------------------------------------


def test_run_end_carries_the_completeness_figures_with_their_summary_and_the_warnings(letterbox_video, tmp_path, fake_parakeet):
    events = []
    cfg = Config(asr="parakeet", layout_samples=4)  # 5 of 12 words filled in: above the limit, kept with the warning
    _, run_dir = run_pipeline(letterbox_video, tmp_path, cfg, sink=events.append)
    end = events[-1]
    assert end["event"] == "run_end" and end["status"] == "ok"
    c = end["completeness"]
    record = json.loads((run_dir / "run.json").read_text())["completeness"]
    assert {k: v for k, v in c.items() if k != "summary"} == record  # the same figures as run.json
    assert c["filled_words"] == 5 and c["summary"].startswith("cross-checked against the second parakeet pass only")
    asr_end = next(e for e in events if e["event"] == "stage_end" and e["stage"] == "asr")
    assert c["summary"] in asr_end["summary"]  # the line the asr stage shows
    assert end["warnings"][0] == SPEECH_WARNING
    assert "more than the usual 3.0%" in end["warnings"][-1] and end["warnings"][-1] == c["warning"]
    assert c["above_limit"] is True and c["limit"] == 0.03 and end.get("error_kind") is None


def test_a_refused_transcript_s_figures_are_in_the_failed_run_end(letterbox_video, tmp_path, fake_parakeet):
    events = []
    with pytest.raises(StageError):
        run_pipeline(letterbox_video, tmp_path, Config(asr="parakeet", layout_samples=4, strict_completeness=True),
                     sink=events.append)
    end = events[-1]
    assert end["status"] == "failed" and "gate-failed" in end["error"]
    assert end["completeness"]["filled_words"] == 5 and "filled 5 words" in end["completeness"]["summary"]
    assert end["warnings"] == [SPEECH_WARNING]  # the asr.parakeet artifact exists; the refused transcript is no artifact
    # the kind of failure, for hosts: in the failed stage's stage_end and in run_end
    asr_end = next(e for e in events if e["event"] == "stage_end" and e["stage"] == "asr")
    assert asr_end["status"] == "failed" and asr_end["error_kind"] == end["error_kind"] == "incomplete_transcript"


def test_the_cli_keeps_the_result_by_default_and_accepts_the_old_allow_incomplete_flag(letterbox_video, tmp_path,
                                                                                      fake_parakeet):
    for extra in ([], ["--allow-incomplete"]):  # deprecated: accepted, no effect, says nothing
        res = runner.invoke(app, ["run", str(letterbox_video), "--out", str(tmp_path / "o"), "--asr", "parakeet",
                                  "--progress", "json", *extra])
        end = json.loads(res.stdout.splitlines()[-1])
        assert res.exit_code == 0 and end["status"] == "ok" and end["completeness"]["above_limit"]
        assert "allow-incomplete" not in res.stderr
    res = runner.invoke(app, ["run", str(letterbox_video), "--out", str(tmp_path / "o"), "--asr", "parakeet",
                              "--progress", "json", "--strict-completeness"])
    end = json.loads(res.stdout.splitlines()[-1])
    assert res.exit_code == 2 and end["status"] == "failed" and end["error_kind"] == "incomplete_transcript"
    assert (tmp_path / "o/letterbox/work/transcript.gate-failed.json").is_file()


def test_run_dir_puts_the_results_in_exactly_that_folder(letterbox_video, tmp_path, fake_parakeet):
    events = []
    chosen = tmp_path / "letterbox 2"
    _, run_dir = run_pipeline(letterbox_video, tmp_path, Config(asr="parakeet", layout_samples=4), sink=events.append,
                              run_dir=chosen)
    assert run_dir == chosen and (chosen / "report.md").is_file() and not (tmp_path / "letterbox").exists()
    assert events[0]["run_dir"] == events[-1]["run_dir"] == str(chosen)
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    res = runner.invoke(app, ["run", str(junk), "--out", str(tmp_path / "o"), "--run-dir", str(tmp_path / "mine"),
                              "--progress", "json"])
    lines = [json.loads(line) for line in res.stdout.splitlines()]
    assert res.exit_code == 2 and lines[0]["run_dir"] == str(tmp_path / "mine") and lines[-1]["error_kind"] is None


def test_without_a_cross_checked_transcript_run_end_has_no_completeness(letterbox_video, tmp_path):
    srt = tmp_path / "talk.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello there.\n")
    events = []
    run_pipeline(letterbox_video, tmp_path, Config(transcript=srt, layout_samples=4), sink=events.append)
    assert events[-1]["completeness"] is None and events[-1]["warnings"] == []
    failed = []
    with pytest.raises(StageError):
        run_pipeline(tmp_path / "missing.mp4", tmp_path, Config(layout_samples=4), sink=failed.append)
    assert [e["event"] for e in failed] == ["run_start", "run_end"]
    assert failed[-1]["completeness"] is None and failed[-1]["warnings"] == []


# ---- setup --progress json ---------------------------------------------------------------------------------------


@pytest.fixture
def machine(monkeypatch, tmp_path):
    model = tmp_path / "snapshot"
    model.mkdir()
    files = {"model.safetensors": model / "model.safetensors", "config.json": model / "config.json"}
    for p in files.values():
        p.write_text("x")
    state = {"model": files}
    monkeypatch.setattr(doctor, "_machine", lambda: ("Darwin", "arm64"))
    monkeypatch.setattr(doctor, "_model_files", lambda repo: state["model"])
    monkeypatch.setattr(doctor, "_model_hash_problem", lambda repo, files: None)  # the files match the pinned hashes
    monkeypatch.setattr(doctor, "_hf_cache_dir", lambda: tmp_path / "hub")
    monkeypatch.setattr(hub, "cache_dir", lambda: Path("/cache"))
    monkeypatch.setattr(hub, "free_gb", lambda path: 50.0)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    state["present"] = files
    return state


def events_of(res):
    return [json.loads(line) for line in res.stdout.splitlines()]  # every stdout line is one JSON object


def test_setup_json_reports_download_progress_and_writes_only_events_to_stdout(machine, monkeypatch):
    machine["model"] = None
    calls = []

    def download(repo, on_progress=None):
        calls.append(repo)
        on_progress("config.json", 0, 100)
        on_progress("config.json", 100, 100)
        on_progress("model.safetensors", 1_000, 2_000)
        machine["model"] = machine["present"]
        return []

    monkeypatch.setattr(hub, "download_model", download)
    res = runner.invoke(app, ["setup", "--progress", "json"])
    assert res.exit_code == 0, res.output
    events = events_of(res)
    assert [e["event"] for e in events] == ["setup_start", "setup_progress", "setup_progress", "setup_progress", "setup_end"]
    start = events[0]
    assert start["size_gb"] == hub.MODEL_SIZE_GB and start["free_gb"] == 50.0 and start["files"] == list(hub.MODEL_FILES)
    assert [(e["file"], e["done"], e["total"]) for e in events[1:4]] == [
        ("config.json", 0, 100), ("config.json", 100, 100), ("model.safetensors", 1000, 2000)]
    assert events[-1]["status"] == "ok" and events[-1]["error"] is None
    assert "speech model ready" in res.stderr and "Downloading" in res.stderr and len(calls) == 1


@pytest.mark.parametrize("args, model, status, code", [
    (["setup"], "present", "present", 0),
    (["setup", "--check"], None, "missing", 1),
])
def test_setup_json_ends_with_one_event_when_nothing_is_downloaded(machine, monkeypatch, args, model, status, code):
    machine["model"] = machine["present"] if model else None
    monkeypatch.setattr(hub, "download_model", lambda repo, on_progress=None: pytest.fail("no download expected"))
    res = runner.invoke(app, [*args, "--progress", "json"])
    assert res.exit_code == code
    events = events_of(res)
    assert [e["event"] for e in events] == ["setup_end"] and events[0]["status"] == status


def test_setup_json_reports_a_failed_download_as_an_event_and_on_stderr(machine, monkeypatch):
    machine["model"] = None

    def broken(repo, on_progress=None):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(hub, "download_model", broken)
    res = runner.invoke(app, ["setup", "--progress", "json"])
    assert res.exit_code == 1
    end = events_of(res)[-1]
    assert end["event"] == "setup_end" and end["status"] == "failed" and "no route to host" in end["error"]
    assert "download failed" in res.stderr


def test_setup_json_reports_too_little_disk_space_without_downloading(machine, monkeypatch):
    machine["model"] = None
    monkeypatch.setattr(hub, "free_gb", lambda path: 3.2)
    monkeypatch.setattr(hub, "download_model", lambda repo, on_progress=None: pytest.fail("no download expected"))
    res = runner.invoke(app, ["setup", "--progress", "json"])
    assert res.exit_code == 1
    assert [(e["event"], e.get("status")) for e in events_of(res)] == [("setup_start", None), ("setup_end", "failed")]


def test_an_interrupted_download_ends_with_an_event_and_exit_130(machine, monkeypatch):
    machine["model"] = None

    def interrupted(repo, on_progress=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(hub, "download_model", interrupted)
    res = runner.invoke(app, ["setup", "--progress", "json"])
    assert res.exit_code == 130 and events_of(res)[-1]["status"] == "interrupted"


def test_the_progress_reporter_counts_bytes_draws_nothing_and_throttles():
    clock = [0.0]
    calls = []
    cls = hub.progress_reporter("model.safetensors", lambda *a: calls.append(a), interval=0.5, now=lambda: clock[0])
    bar = cls(total=1000, unit="B", unit_scale=True, desc="model.safetensors")
    for step in range(9):
        clock[0] = step * 0.2
        bar.update(100)
    clock[0] = 2.0
    bar.update(100)  # complete: always reported
    bar.close()
    assert calls[0] == ("model.safetensors", 0, 1000)
    assert calls[-1] == ("model.safetensors", 1000, 1000)
    assert [c[1] for c in calls] == [0, 400, 700, 1000]  # 0.0 s, 0.6 s, 1.2 s (every 0.5 s or more), then complete


def test_only_the_first_bar_of_a_file_reports():
    """A Xet download makes a second bar for the bytes received; its figures must not interleave with the first's."""
    calls = []
    cls = hub.progress_reporter("model.safetensors", lambda *a: calls.append(a), interval=0.0)
    written, received = cls(total=1000), cls(total=1000)
    received.update(900)
    written.update(500)
    assert calls == [("model.safetensors", 0, 1000), ("model.safetensors", 500, 1000)]


def test_python_dash_m_framescribe_is_the_command_line():
    res = subprocess.run([sys.executable, "-m", "framescribe", "--version"], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and res.stdout.strip() == f"framescribe {__version__}"
