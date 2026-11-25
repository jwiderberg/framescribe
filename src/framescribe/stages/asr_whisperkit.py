"""Backend `whisperkit`: an external whisperkit-cli run as a subprocess, its JSON report parsed."""

from __future__ import annotations

import hashlib
import json
import os
import pty
import re
import select
import shutil
import subprocess
import time
from pathlib import Path
from typing import IO

from ..config import ENV_WHISPERKIT_CLI, ENV_WHISPERKIT_MODEL, Config
from ..errors import NoAudioError
from ..ffmpeg import wav_seconds
from ..fmt import timestamp
from ..models import AsrArtifact, IngestArtifact, Word
from ..proc import child_process
from ..words import (
    EPSILON,
    PAST_END_SECONDS,
    attach_leading_punctuation,
    finalize_words,
    segment_sentences,
)
from .base import Context, Stage, StageError

VERSION = 4
NAME = "asr.whisperkit"
ASSUMED_REALTIME_FACTOR = 7.0  # measured for the debug build in docs/MEASUREMENTS.md
_PERCENT = re.compile(r"\]\s*(\d{1,3})%")
WHISPER_SAMPLE_RATE = 16000  # a segment's `seek` counts samples at this rate
CLI_FLAGS = ("--word-timestamps", "--verbose", "--report")
INPUTS_FILE = "inputs.json"  # next to the report: what it was made from, so it can be parsed again without the CLI


def unresolved_reason(cfg: Config) -> str | None:
    """Why WhisperKit cannot run with this config, or None when both the CLI and the model resolve."""
    cli, model = cfg.whisperkit_cli, cfg.whisperkit_model
    if not cli:
        return f"no CLI configured (--whisperkit-cli or {ENV_WHISPERKIT_CLI})"
    if not model:
        return f"no model directory configured (--whisperkit-model or {ENV_WHISPERKIT_MODEL})"
    if not (Path(cli).is_file() and os.access(cli, os.X_OK)) and shutil.which(cli) is None:
        return f"CLI not found or not executable: {cli}"
    if not Path(model).is_dir():
        return f"model directory not found: {model}"
    return None


def parse_report(report: dict, audio_seconds: float) -> tuple[list[Word], list[Word], list[Word]]:
    """Words from a whisperkit-cli `--word-timestamps --report` JSON report, the words dropped at window restarts,
    and the words dropped because they start after the audio end (see words.finalize_words).

    WhisperKit decodes 30 s windows (a segment's `seek` is where its window starts). When a window restarts
    earlier than the previous one ended, the previous window's trailing words (typically a hallucinated "Okay.")
    lie beyond the restart: the later window is the real decode, so those trailing words are dropped and the later
    window keeps its true timings. A restart may only drop words of the window just before it, and may not start
    before that window does; anything reaching further back raises, as does disorder inside one segment.
    """
    words: list[Word] = []
    seeks: list[int | None] = []  # the window each word came from
    dropped: list[Word] = []
    for seg in report["segments"]:
        if "words" not in seg:
            raise StageError("WhisperKit report has no word timings; was --word-timestamps passed?")
        seg_words = [
            Word(text=w["word"].strip(), start=float(w["start"]), end=float(w["end"]), conf=w.get("probability"))
            for w in seg["words"]
            if w["word"].strip() and not (w["word"].strip().startswith("<|") and w["word"].strip().endswith("|>"))
        ]
        if not seg_words:
            continue
        restart = seg_words[0].start
        n = len(words)
        while n and words[n - 1].start > restart + EPSILON:
            n -= 1
        if n < len(words):
            previous, seek = seeks[-1], seg.get("seek")
            if previous is None or seek is None:
                raise StageError(f"WhisperKit restarts at {restart:.2f} s, but the report gives no window (seek) to check it")
            # a new, later window whose words start inside it, re-decoding the tail of the window just before
            if not (seek > previous and restart >= seek / WHISPER_SAMPLE_RATE - EPSILON and all(s == previous for s in seeks[n:])):
                raise StageError(
                    f"WhisperKit segment at {restart:.2f} s (window from {seek / WHISPER_SAMPLE_RATE:.2f} s) would drop "
                    f"{len(words) - n} earlier words back to {words[n].start:.2f} s, beyond the window before it "
                    f"(from {previous / WHISPER_SAMPLE_RATE:.2f} s)"
                )
            dropped.extend(words[n:])
            del words[n:], seeks[n:]
        words.extend(seg_words)
        seeks.extend([seg.get("seek")] * len(seg_words))
    past_end: list[Word] = []
    return finalize_words(attach_leading_punctuation(words), audio_seconds, past_end), dropped, past_end


def _pump(ctx: Context, proc: subprocess.Popen, master: int, log: IO[bytes], audio_seconds: float) -> None:
    """Copy the CLI's terminal output to the log and turn its progress bar into stage progress."""
    t0 = time.monotonic()
    tail = ""
    percent: int | None = None
    while True:
        ready, _, _ = select.select([master], [], [], 1.0)
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO: the child closed its end
                data = b""
            if not data:
                break
            log.write(data)
            tail = (tail + data.decode(errors="replace"))[-400:]
            found = _PERCENT.findall(tail)
            if found:
                percent = min(100, int(found[-1]))
        elif proc.poll() is not None:
            break
        if percent is not None:
            done = audio_seconds * percent / 100
            note = f"WhisperKit {timestamp(done)} / {timestamp(audio_seconds)}"
        else:
            # no progress output seen yet: an estimate from the measured debug-build speed, labelled as such
            done = min(audio_seconds * 0.95, (time.monotonic() - t0) * ASSUMED_REALTIME_FACTOR)
            note = f"estimate only: no progress from WhisperKit yet, assuming {ASSUMED_REALTIME_FACTOR:.0f}x realtime"
        ctx.progress(done, audio_seconds, "audio_s", note)


def report_inputs(cfg: Config, wav: Path) -> dict:
    """What a report depends on: the audio's content and the CLI invocation (paths of the run aside)."""
    with wav.open("rb") as f:
        audio = hashlib.file_digest(f, "sha256").hexdigest()
    return {"audio_sha256": audio, "cli": cfg.whisperkit_cli, "model": cfg.whisperkit_model, "language": cfg.language,
            "flags": list(CLI_FLAGS)}


def run(ctx: Context) -> AsrArtifact:
    cfg = ctx.config
    if (reason := unresolved_reason(cfg)) is not None:
        raise StageError(f"WhisperKit unavailable: {reason}")
    assert cfg.whisperkit_cli and cfg.whisperkit_model
    ingest = ctx.get("ingest", IngestArtifact)
    if ingest.audio_wav is None:
        raise NoAudioError(f"{ctx.source} has no audio stream, so there is nothing to transcribe; use --transcript")
    wav = ctx.run_dir / ingest.audio_wav
    audio_seconds = wav_seconds(wav)
    report_dir = ctx.work_dir / "whisperkit-report"
    report_file = report_dir / f"{wav.stem}.json"
    inputs_file = report_dir / INPUTS_FILE
    inputs = report_inputs(cfg, wav)
    retained = json.loads(inputs_file.read_text()) if inputs_file.is_file() and report_file.is_file() else None
    if retained is not None and not ctx.forced and {k: retained.get(k) for k in inputs} == inputs:
        # The CLI already transcribed this audio with these options (it takes minutes); only parsing changed.
        wall = float(retained["wall_seconds"])
        ctx.log(f"re-parsing the report whisperkit-cli wrote for this audio and these options ({report_file}); "
                f"the CLI is not run again (use --force {NAME} to run it)")
    else:
        wall = _transcribe(ctx, cfg, wav, report_dir, audio_seconds)
        if not report_file.is_file():
            raise StageError(f"whisperkit-cli wrote no report {report_file}; see {ctx.log_path}")
        inputs_file.write_text(json.dumps({**inputs, "wall_seconds": wall}, indent=1) + "\n")
    report = json.loads(report_file.read_text())  # the raw report stays in work/whisperkit-report/
    words, dropped, past_end = parse_report(report, audio_seconds)
    if dropped:
        ctx.log(
            f"dropped {len(dropped)} out-of-order words from windows that a later window re-decoded: "
            + ", ".join(f"{w.text!r}@{w.start:.2f}" for w in dropped[:20])
            + (f" and {len(dropped) - 20} more" if len(dropped) > 20 else "")
        )
    if past_end:
        ctx.log(f"dropped {len(past_end)} words that start more than {PAST_END_SECONDS:g} s after the audio end "
                f"({audio_seconds:.2f} s): " + ", ".join(f"{w.text!r}@{w.start:.2f}" for w in past_end))
    return AsrArtifact(
        backend="whisperkit",
        model=Path(cfg.whisperkit_model).name,
        language=report.get("language") or cfg.language,
        words=words,
        segments=segment_sentences(words),
        audio_seconds=audio_seconds,
        wall_seconds=wall,
    )


def _transcribe(ctx: Context, cfg: Config, wav: Path, report_dir: Path, audio_seconds: float) -> float:
    """Run whisperkit-cli into a fresh `report_dir`; its wall time in seconds. A failing CLI raises."""
    assert cfg.whisperkit_cli and cfg.whisperkit_model
    shutil.rmtree(report_dir, ignore_errors=True)
    report_dir.mkdir(parents=True)
    cmd = [cfg.whisperkit_cli, "transcribe", "--audio-path", str(wav), "--model-path", cfg.whisperkit_model,
           "--language", cfg.language, *CLI_FLAGS, "--report-path", str(report_dir)]
    ctx.log_path.unlink(missing_ok=True)
    t0 = time.perf_counter()
    # --verbose is what makes the CLI print its progress bar; a pseudo-terminal makes it flush as it goes.
    master, slave = pty.openpty()
    try:
        with ctx.log_path.open("wb") as log, child_process(
            cmd, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave, start_new_session=True
        ) as proc:
            os.close(slave)
            slave = -1
            _pump(ctx, proc, master, log, audio_seconds)
            returncode = proc.wait()
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
    if returncode != 0:
        raise StageError(f"whisperkit-cli failed (exit {returncode}); see {ctx.log_path}")
    return time.perf_counter() - t0


STAGE = Stage(
    name=NAME,
    version=VERSION,
    filename="asr.whisperkit.json",
    model=AsrArtifact,
    run=run,
    config_fields=("whisperkit_cli", "whisperkit_model", "language"),
    deps=("ingest",),
    summary=AsrArtifact.describe,
)
