"""install.sh against stubbed uv, ffmpeg, uname and framescribe on a PATH that holds nothing else, so the machine's
own tools never decide the outcome. Nothing is installed or downloaded."""

import json
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from framescribe.doctor import MISSING, OK, WARNING, Check, as_dict

SCRIPT = Path(__file__).parent.parent / "install.sh"
BASH = shutil.which("bash")
GIT_SOURCE = "git+https://github.com/jwiderberg/framescribe"

pytestmark = pytest.mark.skipif(BASH is None or not SCRIPT.is_file(), reason="needs bash and install.sh")

UNAME = """#!/bin/sh
case "$1" in
  -s) echo "${STUB_OS:-Darwin}" ;;
  -m) echo "${STUB_ARCH:-arm64}" ;;
  *) echo "${STUB_OS:-Darwin}" ;;
esac
"""
LOGGER = """#!/bin/sh
echo "{name} $*" >> "$STUB_LOG"
{extra}
"""


def write_tool(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def world(tmp_path):
    """A PATH directory with symlinks to the few real tools the script needs, plus stubs added per test."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("dirname", "grep", "cat", "env", "sh", "awk"):
        real = shutil.which(tool)
        assert real, tool
        (bin_dir / tool).symlink_to(real)
    write_tool(bin_dir, "uname", UNAME)
    uv_bin = tmp_path / "uvbin"
    uv_bin.mkdir()
    log = tmp_path / "calls.log"

    class World:
        path = bin_dir
        tool_bin = uv_bin
        calls = log
        home = tmp_path / "home"

        @staticmethod
        def add(*names: str, extra: str = "") -> None:
            for name in names:
                write_tool(bin_dir, name, LOGGER.format(name=name, extra=extra))

        @staticmethod
        def everything() -> None:
            """ffmpeg, ffprobe, git, and a uv that installs a `framescribe` stub into `tool_bin`."""
            World.add("ffmpeg", "ffprobe", "git")
            write_tool(bin_dir, "uv", LOGGER.format(name="uv", extra='[ "$1 $2 $3" = "tool dir --bin" ] && echo "$STUB_TOOL_BIN"; true'))
            write_tool(uv_bin, "framescribe", LOGGER.format(name="framescribe", extra=""))

        @staticmethod
        def calls_made() -> list[str]:
            return log.read_text().splitlines() if log.exists() else []

        @staticmethod
        def run(*args, cwd=None, stdin=None, env=None):
            environ = {"PATH": str(bin_dir), "HOME": str(World.home), "STUB_LOG": str(log), "STUB_TOOL_BIN": str(uv_bin)}
            environ.update(env or {})
            return subprocess.run([BASH, str(SCRIPT) if stdin is None else "-s", *(["--"] if stdin is not None else []), *args],
                                  cwd=cwd or SCRIPT.parent, env=environ, input=stdin, capture_output=True, text=True, timeout=60)

    return World


def test_the_script_is_valid_bash_and_ends_with_the_call_of_main():
    assert subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True).returncode == 0
    last = [ln for ln in SCRIPT.read_text().splitlines() if ln.strip()][-1]
    assert last == 'main "$@"'  # a truncated download never reaches this line, so it runs nothing


def test_the_top_comment_says_nothing_runs_until_the_last_line():
    head = "\n".join(SCRIPT.read_text().splitlines()[:20])
    assert "does nothing until its last line" in head and "truncated download" in head


def test_a_truncated_download_runs_nothing(world, tmp_path):
    """Every prefix of the script that stops before the last line has no effect: no stub is called, nothing printed."""
    world.everything()
    lines = SCRIPT.read_text().splitlines()
    for cut in (5, len(lines) // 3, len(lines) // 2, len(lines) - 3):
        res = subprocess.run([BASH, "-s", "--"], input="\n".join(lines[:cut]) + "\n", capture_output=True, text=True,
                             env={"PATH": str(world.path), "HOME": str(world.home), "STUB_LOG": str(world.calls)}, cwd=tmp_path)
        assert res.stdout == "" and world.calls_made() == [], cut


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_shellcheck_passes():
    res = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_help_lists_every_option(world):
    res = world.run("--help")
    assert res.returncode == 0
    for option in ("--no-model", "--install-ffmpeg", "--install-uv", "--dry-run", "--help", "FRAMESCRIBE_SOURCE"):
        assert option in res.stdout


def test_an_unknown_option_is_an_error(world):
    res = world.run("--frobnicate")
    assert res.returncode == 2 and "unknown option: --frobnicate" in res.stderr


def test_dry_run_from_the_clone_prints_the_plan_and_does_nothing(world):
    world.everything()
    res = world.run("--dry-run")
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "[dry-run] nothing will be installed or downloaded" in out
    assert f"[dry-run] would run: uv tool install --force --reinstall --python 3.12 {SCRIPT.parent}" in out  # from the clone
    assert "about 2.5 GB" in out and "at least 6 GB" in out
    assert "[dry-run] would run: framescribe setup" in out
    assert "[dry-run] would run: framescribe doctor" in out
    assert "Next step" in out and "framescribe run recording.mp4 --out out" in out
    assert out.index("tool install") < out.index("framescribe setup") < out.index("framescribe doctor")
    assert world.calls_made() == []  # no stub was executed


def test_dry_run_without_the_model_skips_the_download(world):
    world.everything()
    res = world.run("--dry-run", "--no-model")
    assert res.returncode == 0
    assert "framescribe setup" in res.stdout and "would run: framescribe setup" not in res.stdout
    assert "Skipping the model download" in res.stdout


def test_piped_from_somewhere_else_installs_from_the_git_url(world, tmp_path):
    world.everything()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    res = world.run("--dry-run", cwd=elsewhere, stdin=SCRIPT.read_text())
    assert res.returncode == 0, res.stderr
    assert f"would run: uv tool install --force --reinstall --python 3.12 {GIT_SOURCE}" in res.stdout


def test_piped_inside_a_clone_still_installs_from_the_git_url(world):
    """Standing in a clone is no reason to install from it when the script comes from a pipe."""
    world.everything()
    res = world.run("--dry-run", cwd=SCRIPT.parent, stdin=SCRIPT.read_text())
    assert res.returncode == 0, res.stderr
    assert f"--reinstall --python 3.12 {GIT_SOURCE}\n" in res.stdout
    assert f"--python 3.12 {SCRIPT.parent}" not in res.stdout


def test_a_piped_script_honours_the_source_variable(world):
    world.everything()
    res = world.run("--dry-run", cwd=SCRIPT.parent, stdin=SCRIPT.read_text(), env={"FRAMESCRIBE_SOURCE": "/some/where"})
    assert "--reinstall --python 3.12 /some/where\n" in res.stdout


def test_the_script_file_run_from_another_directory_installs_from_its_own_clone(world, tmp_path):
    world.everything()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    res = world.run("--dry-run", cwd=elsewhere)
    assert f"--reinstall --python 3.12 {SCRIPT.parent}\n" in res.stdout


def test_the_source_can_be_overridden(world):
    world.everything()
    res = world.run("--dry-run", env={"FRAMESCRIBE_SOURCE": "git+https://example.invalid/fork@branch"})
    assert "--reinstall --python 3.12 git+https://example.invalid/fork@branch" in res.stdout


def test_git_is_needed_only_for_a_git_source(world, tmp_path):
    world.add("ffmpeg", "ffprobe", "uv")  # no git
    ok = world.run("--dry-run", "--no-model")
    assert ok.returncode == 0  # a clone needs no git
    res = world.run("--dry-run", env={"FRAMESCRIBE_SOURCE": GIT_SOURCE})
    assert res.returncode == 1 and "git is needed" in res.stderr and "xcode-select --install" in res.stderr


@pytest.mark.parametrize("missing", [("ffmpeg",), ("ffprobe",), ("ffmpeg", "ffprobe")])
def test_missing_ffmpeg_prints_the_brew_command_and_fails(world, missing):
    world.add(*{"ffmpeg", "ffprobe", "uv", "git"} - set(missing))
    res = world.run("--dry-run")
    assert res.returncode == 1
    assert "brew install ffmpeg" in res.stderr and "--install-ffmpeg" in res.stderr
    assert all(m in res.stderr for m in missing)
    assert "tool install" not in res.stdout  # stopped before installing anything


def test_install_ffmpeg_needs_homebrew(world):
    world.add("uv", "git")
    res = world.run("--dry-run", "--install-ffmpeg")
    assert res.returncode == 1 and "Homebrew" in res.stderr


def test_install_ffmpeg_with_homebrew_plans_the_brew_command(world):
    world.add("uv", "git", "brew")
    res = world.run("--dry-run", "--install-ffmpeg")
    assert res.returncode == 0, res.stderr
    assert "would run: brew install ffmpeg" in res.stdout
    assert "sudo" not in res.stdout


def test_missing_uv_prints_the_official_install_command_and_fails(world):
    world.add("ffmpeg", "ffprobe", "git")
    res = world.run("--dry-run")
    assert res.returncode == 1
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in res.stderr and "--install-uv" in res.stderr


def test_install_uv_plans_the_installer_without_touching_the_shell_profile(world):
    world.add("ffmpeg", "ffprobe", "git", "curl")
    res = world.run("--dry-run", "--install-uv")
    assert res.returncode == 0, res.stderr
    planned = next(ln for ln in res.stdout.splitlines() if "astral.sh" in ln)
    assert "UV_NO_MODIFY_PATH=1" in planned and "would run" in planned


@pytest.mark.parametrize("system,arch,message", [
    ("Darwin", "x86_64", "Apple Silicon"), ("Linux", "x86_64", "macOS"), ("Linux", "aarch64", "macOS")])
def test_other_platforms_are_refused_before_anything_else(world, system, arch, message):
    world.everything()
    res = world.run("--dry-run", env={"STUB_OS": system, "STUB_ARCH": arch})
    assert res.returncode == 1 and message in res.stderr
    assert "tool install" not in res.stdout and "ok: ffmpeg" not in res.stdout


def test_a_real_run_installs_downloads_the_model_and_checks_in_that_order(world):
    world.everything()
    res = world.run()
    assert res.returncode == 0, res.stderr
    assert world.calls_made() == [f"uv tool install --force --reinstall --python 3.12 {SCRIPT.parent}", "uv tool dir --bin",
                                  "framescribe setup", "framescribe doctor"]
    assert f"export PATH=\"{world.tool_bin}:$PATH\"" in res.stdout  # the bin directory is not on PATH: told, not edited
    assert not world.home.exists()  # nothing was written to the home directory, so no profile either
    assert "Next step: transcribe a recording." in res.stdout


def test_a_real_run_without_the_model_does_not_download_it(world):
    world.everything()
    res = world.run("--no-model")
    assert res.returncode == 0
    assert world.calls_made() == [f"uv tool install --force --reinstall --python 3.12 {SCRIPT.parent}", "uv tool dir --bin", "framescribe doctor"]


def test_a_failing_download_stops_the_script(world):
    world.everything()
    write_tool(world.tool_bin, "framescribe", LOGGER.format(name="framescribe", extra='[ "$1" = setup ] && exit 1; true'))
    res = world.run()
    assert res.returncode == 1 and "model download did not finish" in res.stderr
    assert "framescribe doctor" not in world.calls_made()


def failing_doctor(world, tmp_path, *checks: Check, raw: str | None = None) -> None:
    """A `framescribe` stub whose `doctor` exits 1 and, with --json, prints what the real doctor would print."""
    report = tmp_path / "doctor.json"
    report.write_text(raw if raw is not None else json.dumps(as_dict(list(checks)), indent=2))
    extra = '[ "$1" = doctor ] && { [ "$2" = --json ] && cat "$STUB_DOCTOR_JSON"; exit 1; }; true'
    write_tool(world.tool_bin, "framescribe", LOGGER.format(name="framescribe", extra=extra))
    world.env = {"STUB_DOCTOR_JSON": str(report)}


GOOD = [Check("platform", OK, "macOS"), Check("ffmpeg", OK, "7.1"), Check("disk space", WARNING, "low", "free some"),
        Check("whisperkit", MISSING, "not configured", None, required=False)]
NO_MODEL = Check("speech model", MISSING, "not cached, with \"quotes\" and a: colon", "framescribe setup")


def test_a_failing_doctor_fails_the_script_when_the_model_was_downloaded(world, tmp_path):
    world.everything()
    failing_doctor(world, tmp_path, *GOOD, NO_MODEL)
    res = world.run(env=world.env)
    assert res.returncode == 1 and "Next step: fix what" in res.stdout


def test_with_no_model_a_doctor_failure_caused_only_by_the_missing_model_is_expected(world, tmp_path):
    world.everything()
    failing_doctor(world, tmp_path, *GOOD, NO_MODEL)
    res = world.run("--no-model", env=world.env)
    assert res.returncode == 0, res.stderr
    assert "only thing missing, as expected with --no-model" in res.stdout and "framescribe setup" in res.stdout
    assert "Next step: fix what" not in res.stdout
    assert world.calls_made()[-2:] == ["framescribe doctor", "framescribe doctor --json"]


@pytest.mark.parametrize("others", [
    [Check("ffmpeg", MISSING, "not found", "brew install ffmpeg")],
    [Check("apple vision ocr", MISSING, "no Vision module")],
    [Check("platform", MISSING, "Linux x86_64"), Check("python", MISSING, "3.9")],
])
def test_with_no_model_any_other_doctor_failure_still_fails_the_script(world, tmp_path, others):
    world.everything()
    failing_doctor(world, tmp_path, *GOOD, NO_MODEL, *others)
    res = world.run("--no-model", env=world.env)
    assert res.returncode == 1 and "Next step: fix what" in res.stdout


def test_with_no_model_a_failure_that_is_not_the_model_fails_even_when_the_model_is_fine(world, tmp_path):
    world.everything()
    failing_doctor(world, tmp_path, *GOOD, Check("ffprobe", MISSING, "not found", "brew install ffmpeg"))
    assert world.run("--no-model", env=world.env).returncode == 1


@pytest.mark.parametrize("raw", ["", "not json at all", "{}"])
def test_with_no_model_a_doctor_that_says_nothing_usable_fails_the_script(world, tmp_path, raw):
    world.everything()
    failing_doctor(world, tmp_path, raw=raw)
    assert world.run("--no-model", env=world.env).returncode == 1


def test_the_script_never_calls_sudo_or_edits_a_profile():
    text = SCRIPT.read_text()
    assert not re.search(r"(^|[;&|(]|\brun)\s*sudo\b", text, re.MULTILINE)  # never in command position
    for profile in (".zshrc", ".bashrc", ".bash_profile", ".zprofile", ".profile"):
        assert profile not in text
