"""`doctor`, `setup`, `--version` and the checks `run` makes first. No network, no model, no real machine probing."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from framescribe import __version__, doctor, hub
from framescribe.cli import app
from framescribe.config import ENV_WHISPERKIT_CLI, ENV_WHISPERKIT_MODEL, Config

runner = CliRunner()
REPO = Config().parakeet_model


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """`run` switches Hugging Face to offline mode for its process; keep that, and the WhisperKit variables, out of
    the other tests."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.delenv(ENV_WHISPERKIT_CLI, raising=False)
    monkeypatch.delenv(ENV_WHISPERKIT_MODEL, raising=False)


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """Everything present on a Mac with an M-series chip; tests then break one thing at a time."""
    model = tmp_path / "snapshot"
    model.mkdir()
    (model / "model.safetensors").write_text("x")
    (model / "config.json").write_text("{}")
    state = {
        "machine": ("Darwin", "arm64"), "python": (3, 12, 4), "tools": {"ffmpeg": "/bin/ffmpeg", "ffprobe": "/bin/ffprobe"},
        "versions": {"/bin/ffmpeg": "7.1", "/bin/ffprobe": "7.1"},
        "model": {"model.safetensors": model / "model.safetensors", "config.json": model / "config.json"},
        "vision": None, "disk": 100.0, "memory": (36.0, 60),
    }
    monkeypatch.setattr(doctor, "_machine", lambda: state["machine"])
    monkeypatch.setattr(doctor, "_python_version", lambda: state["python"])
    monkeypatch.setattr(doctor, "_which", lambda name: state["tools"].get(name))
    monkeypatch.setattr(doctor, "_tool_version", lambda path: state["versions"].get(path))
    monkeypatch.setattr(doctor, "_model_files", lambda repo: state["model"])
    monkeypatch.setattr(doctor, "_hf_cache_dir", lambda: tmp_path / "hub")
    monkeypatch.setattr(doctor, "_vision_importable", lambda: state["vision"])
    monkeypatch.setattr(doctor, "_free_disk_gb", lambda path: state["disk"])
    monkeypatch.setattr(doctor, "_memory", lambda: state["memory"])
    return state


def by_name(checks):
    return {c.name: c for c in checks}


def test_everything_present_exits_zero_with_one_line_per_check(machine):
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 0, res.output
    lines = [ln for ln in res.output.splitlines() if ln.strip()]
    assert len(lines) == 9 + 1  # one line per check, then the verdict
    assert all(ln.startswith("ok ") for ln in lines[:9])
    assert "ffmpeg" in res.output and "7.1" in res.output and "/bin/ffmpeg" in res.output
    assert "not configured (optional)" in res.output
    assert lines[-1] == "everything required is present"


def test_json_output_is_machine_readable(machine):
    res = runner.invoke(app, ["doctor", "--json"])
    data = json.loads(res.output)
    assert res.exit_code == 0 and data["ok"] is True
    assert [c["name"] for c in data["checks"]] == [
        "platform", "python", "ffmpeg", "ffprobe", "speech model", "apple vision ocr", "disk space", "memory", "whisperkit"]
    assert all(set(c) == {"name", "status", "detail", "fix", "required"} for c in data["checks"])
    assert by_name_json(data)["whisperkit"]["required"] is False


def by_name_json(data):
    return {c["name"]: c for c in data["checks"]}


@pytest.mark.parametrize("key,value,name,fix", [
    ("machine", ("Linux", "x86_64"), "platform", "M-series"),
    ("machine", ("Darwin", "x86_64"), "platform", "Rosetta"),
    ("python", (3, 11, 9), "python", "Python 3.12"),
    ("tools", {"ffprobe": "/bin/ffprobe"}, "ffmpeg", "brew install ffmpeg"),
    ("tools", {"ffmpeg": "/bin/ffmpeg"}, "ffprobe", "brew install ffmpeg"),
    ("model", None, "speech model", "framescribe setup"),
    ("vision", "ModuleNotFoundError: No module named 'Vision'", "apple vision ocr", "reinstall"),
])
def test_each_missing_requirement_exits_one_and_says_how_to_fix_it(machine, key, value, name, fix):
    machine[key] = value
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 1, res.output
    line = next(ln for ln in res.output.splitlines() if ln.startswith("missing") and name in ln)
    assert line.startswith("missing ") and "fix:" in line and fix in line
    assert "something required is missing" in res.output
    data = json.loads(runner.invoke(app, ["doctor", "--json"]).output)
    assert data["ok"] is False and by_name_json(data)[name]["status"] == "missing" and fix in by_name_json(data)[name]["fix"]


def test_a_missing_model_names_the_repository_and_the_size(machine):
    machine["model"] = None
    c = by_name(doctor.run_checks())["speech model"]
    assert REPO in c.detail and "2.5 GB" in c.detail and c.fix == "framescribe setup"


def test_low_disk_and_low_memory_are_warnings_and_do_not_fail(machine):
    machine["disk"] = 1.0
    machine["memory"] = (36.0, 5)
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 0, res.output
    lines = {ln.split()[1] + " " + ln.split()[2] if ln.split()[1] == "disk" else ln.split()[1]: ln for ln in res.output.splitlines()[:9]}
    assert lines["disk space"].startswith("warning") and "1.0 GB free" in lines["disk space"]
    assert lines["memory"].startswith("warning") and "5% free" in lines["memory"]


def test_the_disk_threshold_is_six_gb_until_the_model_is_present(machine):
    machine["disk"] = 4.0
    assert doctor.check_disk(model_present=False).status == "warning"
    assert doctor.check_disk(model_present=True).status == "ok"


def test_unreadable_memory_figures_are_a_warning(machine):
    machine["memory"] = None
    assert doctor.check_memory().status == "warning"


def test_ffmpeg_that_prints_no_version_is_a_warning_not_a_failure(machine):
    machine["versions"] = {}
    c = doctor.check_ffmpeg_tool("ffmpeg")
    assert c.status == "warning" and not c.blocking


def test_whisperkit_configured_but_missing_is_a_warning_that_names_the_reason(machine, monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_WHISPERKIT_CLI, str(tmp_path / "nowhere" / "whisperkit-cli"))
    monkeypatch.setenv(ENV_WHISPERKIT_MODEL, str(tmp_path))
    res = runner.invoke(app, ["doctor"])
    assert res.exit_code == 0
    line = next(ln for ln in res.output.splitlines() if ln.startswith("warning") and "whisperkit" in ln)
    assert "nowhere" in line or "not found" in line or "not an executable" in line or "no CLI" in line


def test_whisperkit_configured_and_present_is_ok(machine, monkeypatch, tmp_path):
    cli = tmp_path / "whisperkit-cli"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setenv(ENV_WHISPERKIT_CLI, str(cli))
    monkeypatch.setenv(ENV_WHISPERKIT_MODEL, str(tmp_path))
    c = doctor.check_whisperkit()
    assert c.status == "ok" and str(cli) in c.detail


# ---- --version -----------------------------------------------------------------------------------------------------


def test_version_option_prints_the_package_version():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0 and res.output.strip() == f"framescribe {__version__}"


def test_the_package_and_pyproject_versions_agree():
    text = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert f'version = "{__version__}"' in text


# ---- setup ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch):
    """`setup` with a recorder instead of the download."""
    calls = []
    monkeypatch.setattr(hub, "download_model", lambda repo: calls.append(repo) or [])
    return calls


def test_setup_check_reports_a_present_model_with_exit_zero(machine, no_network):
    res = runner.invoke(app, ["setup", "--check"])
    assert res.exit_code == 0 and "speech model present" in res.output and no_network == []


def test_setup_check_reports_a_missing_model_with_exit_one_and_downloads_nothing(machine, no_network):
    machine["model"] = None
    res = runner.invoke(app, ["setup", "--check"])
    assert res.exit_code == 1 and "speech model missing" in res.output and "framescribe setup" in res.output
    assert no_network == []


def test_setup_with_the_model_present_downloads_nothing(machine, no_network):
    res = runner.invoke(app, ["setup"])
    assert res.exit_code == 0 and "speech model present" in res.output and no_network == []


def test_setup_states_the_size_and_downloads_a_missing_model(machine, no_network, monkeypatch):
    present = machine["model"]
    machine["model"] = None
    monkeypatch.setattr(hub, "cache_dir", lambda: Path("/cache"))
    monkeypatch.setattr(hub, "free_gb", lambda path: 50.0)
    monkeypatch.delenv("HF_HUB_OFFLINE")
    monkeypatch.setattr(hub, "download_model", lambda repo: no_network.append(repo) or machine.update(model=present) or [])
    res = runner.invoke(app, ["setup"])
    assert res.exit_code == 0, res.output
    assert no_network == [REPO]
    assert "about 2.5 GB" in res.output and "50.0 GB" in res.output and "speech model ready" in res.output


def test_setup_stops_with_the_figure_when_the_disk_is_too_small(machine, no_network, monkeypatch):
    machine["model"] = None
    monkeypatch.setattr(hub, "free_gb", lambda path: 3.2)
    monkeypatch.delenv("HF_HUB_OFFLINE")
    res = runner.invoke(app, ["setup"])
    assert res.exit_code == 1 and "3.2 GB" in res.output and "6 GB" in res.output and no_network == []


def test_setup_refuses_when_offline_mode_is_set(machine, no_network, monkeypatch):
    machine["model"] = None
    monkeypatch.setattr(hub, "free_gb", lambda path: 50.0)
    res = runner.invoke(app, ["setup"])  # the fixture sets HF_HUB_OFFLINE=1
    assert res.exit_code == 1 and "HF_HUB_OFFLINE" in res.output and no_network == []


def test_setup_reports_a_failed_download_and_exits_one(machine, monkeypatch):
    machine["model"] = None
    monkeypatch.setattr(hub, "free_gb", lambda path: 50.0)
    monkeypatch.delenv("HF_HUB_OFFLINE")

    def broken(repo):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(hub, "download_model", broken)
    res = runner.invoke(app, ["setup"])
    assert res.exit_code == 1 and "download failed" in res.output and "no route to host" in res.output


def test_setup_refuses_on_an_unsupported_platform(machine, no_network):
    machine["machine"] = ("Linux", "x86_64")
    res = runner.invoke(app, ["setup", "--check"])
    assert res.exit_code == 1 and "Apple Silicon" in res.output and no_network == []


# ---- run fails fast ------------------------------------------------------------------------------------------------


@pytest.fixture
def video(tmp_path):
    f = tmp_path / "talk.mp4"
    f.write_bytes(b"not a video")  # the start-up checks come first, so the file is never opened
    return f


def started(monkeypatch):
    """Make any attempt to start the pipeline visible."""
    calls = []
    monkeypatch.setattr("framescribe.cli.run_pipeline", lambda *a, **k: calls.append(a))
    return calls


def test_run_without_the_model_says_to_run_setup_and_starts_nothing(machine, video, tmp_path, monkeypatch):
    machine["model"] = None
    calls = started(monkeypatch)
    res = runner.invoke(app, ["run", str(video), "--out", str(tmp_path / "o")])
    assert res.exit_code == 2
    assert "speech model" in res.output and "framescribe setup" in res.output and "doctor" in res.output
    assert calls == [] and not (tmp_path / "o").exists()


def test_run_without_ffmpeg_says_how_to_install_it_and_starts_nothing(machine, video, tmp_path, monkeypatch):
    machine["tools"] = {"ffprobe": "/bin/ffprobe"}
    calls = started(monkeypatch)
    res = runner.invoke(app, ["run", str(video), "--out", str(tmp_path / "o")])
    assert res.exit_code == 2 and "ffmpeg" in res.output and "brew install ffmpeg" in res.output and calls == []


def test_run_without_ffprobe_says_so(machine, video, tmp_path, monkeypatch):
    machine["tools"] = {"ffmpeg": "/bin/ffmpeg"}
    calls = started(monkeypatch)
    res = runner.invoke(app, ["run", str(video), "--out", str(tmp_path / "o")])
    assert res.exit_code == 2 and "ffprobe" in res.output and calls == []


@pytest.mark.parametrize("system,machine_name", [("Linux", "x86_64"), ("Darwin", "x86_64"), ("Linux", "aarch64")])
def test_run_refuses_anything_but_macos_on_apple_silicon(machine, video, tmp_path, monkeypatch, system, machine_name):
    machine["machine"] = (system, machine_name)
    calls = started(monkeypatch)
    res = runner.invoke(app, ["run", str(video), "--out", str(tmp_path / "o")])
    assert res.exit_code == 2 and "Apple Silicon" in res.output and f"{system} {machine_name}" in res.output and calls == []


@pytest.mark.parametrize("extra", [["--transcript", "t.srt"], ["--asr", "whisperkit"]])
def test_run_does_not_need_the_speech_model_when_it_will_not_decode_speech(machine, video, tmp_path, monkeypatch, extra):
    machine["model"] = None
    calls = started(monkeypatch)
    srt = tmp_path / "t.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    extra = [str(srt) if x == "t.srt" else x for x in extra]
    res = runner.invoke(app, ["run", str(video), "--out", str(tmp_path / "o"), *extra])
    assert res.exit_code == 0, res.output
    assert len(calls) == 1


def test_run_with_everything_present_starts_the_pipeline(machine, video, tmp_path, monkeypatch):
    calls = started(monkeypatch)
    res = runner.invoke(app, ["run", str(video), "--out", str(tmp_path / "o")])
    assert res.exit_code == 0, res.output
    assert len(calls) == 1


def test_run_switches_hugging_face_to_offline_mode(machine, video, tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HUB_OFFLINE")
    started(monkeypatch)
    runner.invoke(app, ["run", str(video), "--out", str(tmp_path / "o")])
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    monkeypatch.delenv("HF_HUB_OFFLINE")  # the variable is the process's own now: do not leak it into other tests


def test_the_model_check_answers_from_the_local_cache_in_offline_mode():
    """`cached_model_files` for a repository that is not cached answers None; offline mode is on, so any attempt to
    reach the network would raise instead."""
    assert hub.cached_model_files("some-owner/some-model") is None
