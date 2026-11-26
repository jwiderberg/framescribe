"""Command line: `framescribe run`, `setup` and `doctor`."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import typer

from . import __version__, doctor as doctor_checks, hub
from .config import ENV_WHISPERKIT_CLI, ENV_WHISPERKIT_MODEL, AsrMode, Config
from .fmt import terminal_text
from .pipeline import FORCE_ALL, run_pipeline
from .proc import Terminated, install_termination_handlers
from .stages.base import StageError
from .status import ProgressMode, escape_hidden, make_renderer

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Turn a long screen recording with spoken commentary into a Markdown evidence report.")


def _parse_rect(value: str | None) -> tuple[int, int, int, int] | None:
    if value is None:
        return None
    try:
        x, y, w, h = (int(p) for p in value.split(","))
    except ValueError:
        raise typer.BadParameter("expected four integers: x,y,w,h") from None
    return x, y, w, h


def _show_version(value: bool) -> None:
    if value:
        _echo(f"framescribe {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[bool, typer.Option("--version", callback=_show_version, is_eager=True, help="Show the version and exit.")] = False,
) -> None:
    pass


def _echo(text: object, err: bool = False) -> None:
    """Every line the commands print goes through here: no control or invisible format character from a path, an
    environment variable or a message reaches the terminal (fmt.terminal_text). JSON events are printed as data."""
    typer.echo(terminal_text(text, keep_newlines=True), err=err)


def _fail(message: str, code: int = 2) -> typer.Exit:
    _echo(f"error: {message}", err=True)
    return typer.Exit(code)


@app.command()
def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Check what framescribe needs: platform, ffmpeg, the speech model, Apple Vision, disk and memory.

    Exit status 0 when everything required is present, 1 otherwise. Does not use the network.
    """
    checks = doctor_checks.run_checks(Config().with_env_defaults())
    if as_json:
        typer.echo(json.dumps(doctor_checks.as_dict(checks), indent=2))  # ensure_ascii: every character escaped
    else:
        _echo(doctor_checks.format_text(checks))
    raise typer.Exit(0 if doctor_checks.all_required_ok(checks) else 1)


SetupProgress = Literal["auto", "json"]


@app.command()
def setup(
    check: Annotated[bool, typer.Option("--check", help="Only report whether the speech model is present; download nothing.")] = False,
    progress: Annotated[
        SetupProgress,
        typer.Option(
            "--progress",
            help="auto: messages and Hugging Face's progress bars; json: one event per line on stdout (messages go to stderr).",
        ),
    ] = "auto",
) -> None:
    """Download the speech model (about 2.5 GB) into the Hugging Face cache.

    The only command that uses the network. `run` never downloads anything.
    """
    as_json = progress == "json"
    t_start = time.perf_counter()

    def emit(event: str, **fields: Any) -> None:
        if as_json:
            print(escape_hidden(json.dumps({"event": event, "t": round(time.perf_counter() - t_start, 3), **fields},
                                           ensure_ascii=False)), flush=True)

    def say(text: str) -> None:
        _echo(text, err=as_json)  # stdout carries only events in json mode

    def fail(message: str, code: int = 1) -> typer.Exit:
        emit("setup_end", status="failed", detail=None, error=message)
        return _fail(message, code)

    repo = Config().parakeet_model
    problem = doctor_checks.check_platform()
    if problem.blocking:
        raise fail(f"{problem.detail}. Fix: {problem.fix}")
    present = doctor_checks.check_model(repo, verify=True)
    if present.status == doctor_checks.OK:
        say(f"speech model present: {present.detail}")
        emit("setup_end", status="present", detail=present.detail, error=None)
        raise typer.Exit(0)
    if check:
        say(f"speech model missing: {present.detail}")
        say("run `framescribe setup` to download it")
        emit("setup_end", status="missing", detail=present.detail, error=None)
        raise typer.Exit(1)
    cache = hub.cache_dir()
    free = hub.free_gb(cache)
    say(f"Speech model: {repo}, about {hub.MODEL_SIZE_GB} GB, from Hugging Face into {cache}")
    say(f"Free disk space there: {free:.1f} GB (at least {hub.MIN_FREE_GB:g} GB are needed)")
    emit("setup_start", repo=repo, size_gb=hub.MODEL_SIZE_GB, cache_dir=str(cache), free_gb=round(free, 1),
         files=list(hub.MODEL_FILES))
    if free < hub.MIN_FREE_GB:
        raise fail(f"only {free:.1f} GB free where the model cache is; at least {hub.MIN_FREE_GB:g} GB are needed. Free some space and run `framescribe setup` again.")
    if hub.offline_requested():
        raise fail("HF_HUB_OFFLINE is set, which forbids the download. Unset it and run `framescribe setup` again.")
    for removed in hub.discard_mismatched(repo):
        say(f"removed a cached model file that is not the pinned one: {removed}")
    say(f"Downloading revision {hub.revision(repo) or 'main'} (this is the only network access framescribe makes) ...")
    try:
        if as_json:
            hub.download_model(repo, on_progress=lambda name, done, total: emit("setup_progress", file=name, done=done, total=total))
        else:
            hub.download_model(repo)
    except KeyboardInterrupt:
        emit("setup_end", status="interrupted", detail=None, error="interrupted")
        raise _fail("interrupted; run `framescribe setup` again to resume", 130) from None
    except Exception as e:  # network errors of every kind, interrupted downloads, full disks
        raise fail(f"the download failed: {type(e).__name__}: {e}. Run `framescribe setup` again to resume.") from None
    done = doctor_checks.check_model(repo, verify=True)
    if done.status != doctor_checks.OK:
        raise fail(f"the download finished but the model is not usable: {done.detail}")
    say(f"speech model ready: {done.detail}")
    emit("setup_end", status="ok", detail=done.detail, error=None)


@app.command()
def run(
    video: Annotated[Path, typer.Argument(help="Source video file.")],
    out: Annotated[Path, typer.Option("--out", help="Output root; results go to OUT/<video-stem>/.")] = Path("out"),
    run_dir: Annotated[Optional[Path], typer.Option(
        "--run-dir", help="Put the results in exactly this folder instead of OUT/<video-stem>/ (for hosts that choose "
                          "the folder themselves).")] = None,
    asr: Annotated[AsrMode, typer.Option("--asr", help="ASR backend. auto: parakeet, plus whisperkit when it resolves.")] = "auto",
    transcript: Annotated[Optional[Path], typer.Option("--transcript", help="Use this SRT, VTT or transcript JSON instead of running ASR.")] = None,
    content_rect: Annotated[Optional[str], typer.Option("--content-rect", help="Override layout detection: x,y,w,h.")] = None,
    whisperkit_cli: Annotated[Optional[str], typer.Option("--whisperkit-cli", help=f"whisperkit-cli executable (env {ENV_WHISPERKIT_CLI}).")] = None,
    whisperkit_model: Annotated[Optional[str], typer.Option("--whisperkit-model", help=f"WhisperKit model directory (env {ENV_WHISPERKIT_MODEL}).")] = None,
    max_filled_share: Annotated[float, typer.Option(
        "--max-filled-share", min=0.0, max=1.0,
        help="Share of the transcript's words filled in from other sources above which the result carries a "
             "warning.")] = Config.max_filled_share,
    strict_completeness: Annotated[bool, typer.Option(
        "--strict-completeness",
        help="Fail the run instead when the filled-in share is above --max-filled-share.")] = False,
    allow_incomplete: Annotated[bool, typer.Option(
        "--allow-incomplete",
        help="Deprecated, has no effect: a transcript above --max-filled-share is always kept, with a warning.")] = False,
    force: Annotated[
        Optional[list[str]],
        typer.Option(
            "--force",
            help=f"Recompute this stage and everything downstream; repeatable. '{FORCE_ALL}' means every stage.",
        ),
    ] = None,
    progress: Annotated[
        ProgressMode,
        typer.Option(
            "--progress",
            help="Status display: rich on a terminal, plain lines, or json (one event per line on stdout). auto picks rich or plain.",
        ),
    ] = "auto",
) -> None:
    """Run the pipeline. Stages already computed with the same inputs are reused."""
    hub.go_offline()  # `run` never downloads: the model comes from `framescribe setup`
    cfg = Config(
        asr=asr,
        transcript=transcript,
        content_rect=_parse_rect(content_rect),
        whisperkit_cli=whisperkit_cli,
        whisperkit_model=whisperkit_model,
        max_filled_share=max_filled_share,
        strict_completeness=strict_completeness,
    ).with_env_defaults()
    del allow_incomplete  # accepted for old command lines; it was the opposite of --strict-completeness
    if (problem := doctor_checks.start_up_problem(cfg)) is not None:
        raise _fail(problem)
    install_termination_handlers()
    try:
        run_pipeline(video, out, cfg, force or [], sink=make_renderer(progress), run_dir=run_dir)
    except StageError as e:
        _echo(f"error: {e}", err=True)
        raise typer.Exit(2) from None
    except KeyboardInterrupt as e:
        terminated = isinstance(e, Terminated)
        _echo(f"{'terminated' if terminated else 'interrupted'}; finished stages stay cached", err=True)
        raise typer.Exit(128 + e.signum if isinstance(e, Terminated) else 130) from None
