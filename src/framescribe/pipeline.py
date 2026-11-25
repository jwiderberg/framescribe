"""Stage registry and runner."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .cache import ArtifactStore, source_fingerprint, stage_key
from .config import Config
from .ffmpeg import probe
from .models import AsrArtifact, Completeness, IngestArtifact, RunRecord, StageRecord, TranscriptArtifact
from .errors import GateError
from .proc import Terminated
from .render import write_transcript_md
from .safeio import atomic_write_text, check_run_folders
from .stages import asr, asr_parakeet, asr_whisperkit, ingest, ocr, report, visual
from .stages.base import Context, Stage, StageError

FORCE_ALL = "all"
SAFE_RUN_NAME = "video"  # the run folder's name when the video's own name cannot be one
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029/]")


def run_folder_name(source: Path) -> str:
    """The video's name without its extension (`Path.stem`), or SAFE_RUN_NAME when that is empty, "." or "..", or holds
    a path separator, a NUL or another control character (a video called "...mov" has the stem "..")."""
    stem = source.stem
    if stem in ("", ".", "..") or _UNSAFE_NAME.search(stem) or (os.altsep and os.altsep in stem):
        return SAFE_RUN_NAME
    return stem


def resolve_run_dir(source: Path, out: Path, run_dir: Path | None) -> Path:
    """The run folder as an absolute path (no relative path ever reaches ffmpeg, where `md5:x` would be a protocol).
    OUT/<name>/ must be a direct child of OUT once both are resolved; an explicit --run-dir is the caller's choice,
    but it must not be a symbolic link either."""
    if run_dir is not None:
        if str(run_dir).strip() == "":
            raise StageError("--run-dir is empty")
        chosen = Path(os.path.abspath(run_dir))
        if os.path.islink(chosen):
            raise StageError(f"refusing to use the run folder {chosen}: it is a symbolic link")
        return chosen
    root = Path(os.path.abspath(out))
    chosen = root / run_folder_name(source)
    if Path(os.path.realpath(chosen)).parent != Path(os.path.realpath(root)):
        raise StageError(f"the run folder {chosen} would not be inside the output folder {root}")
    return chosen


STAGE_ORDER = ["ingest", asr_parakeet.NAME, asr_whisperkit.NAME, asr.NAME, visual.NAME, ocr.NAME, report.NAME]


@dataclass(frozen=True)
class Plan:
    stages: list[Stage]
    skipped: list[StageRecord] = field(default_factory=list)


def build_plan(cfg: Config) -> Plan:
    """The stages this config runs, in dependency order. Raises StageError for an impossible request."""
    if cfg.transcript is not None:
        if cfg.asr != "auto":
            raise StageError(f"--transcript replaces ASR; it cannot be combined with --asr {cfg.asr}")
        if not Path(cfg.transcript).is_file():
            raise StageError(f"transcript file not found: {cfg.transcript}")
        return Plan([ingest.STAGE, asr.make_stage(()), visual.STAGE, ocr.STAGE, report.STAGE])

    skipped: list[StageRecord] = []
    backends: list[Stage] = []
    if cfg.asr in ("parakeet", "both", "auto"):
        backends.append(asr_parakeet.STAGE)
    if cfg.asr in ("whisperkit", "both", "auto"):
        reason = asr_whisperkit.unresolved_reason(cfg)
        if reason is None:
            backends.append(asr_whisperkit.STAGE)
        elif cfg.asr == "auto":
            skipped.append(
                StageRecord(stage=asr_whisperkit.NAME, status="skipped", wall_seconds=0.0, reason=reason)
            )
        else:
            raise StageError(f"--asr {cfg.asr} needs WhisperKit, but: {reason}")
    names = tuple(s.name for s in backends)
    return Plan([ingest.STAGE, *backends, asr.make_stage(names), visual.STAGE, ocr.STAGE, report.STAGE], skipped)


def downstream_closure(stages: list[Stage], names: Collection[str]) -> set[str]:
    """`names` plus every stage that depends on them, directly or not."""
    out = set(names)
    for s in stages:  # stages are in dependency order
        if out.intersection(s.deps):
            out.add(s.name)
    return out


def forced_stages(plan: Plan, force: Collection[str]) -> set[str]:
    if FORCE_ALL in force:
        return {s.name for s in plan.stages}
    known = {s.name for s in plan.stages}
    unknown = set(force) - known
    if unknown:
        raise StageError(f"--force: unknown stage {sorted(unknown)}; this run has {sorted(known)}")
    return downstream_closure(plan.stages, force)


Sink = Callable[[dict[str, Any]], None]

# Event status vocabulary (docs/DESIGN.md "Live status"); run.json keeps its own hit/miss/forced/skipped.
EVENT_STATUS = {"hit": "cached", "miss": "done", "forced": "done", "skipped": "skipped"}


def run_pipeline(
    source: Path,
    out: Path,
    cfg: Config,
    force: Collection[str] = (),
    sink: Sink = lambda event: None,
    run_dir: Path | None = None,
) -> tuple[RunRecord, Path]:
    """Run all stages for `source` into `out/<stem>/` (or into `run_dir` when given), sending status events to `sink`.

    Events are dicts with "event" (run_start, stage_start, stage_progress, log, stage_end, run_end)
    and "t", seconds since the run started. Returns the run record and the run directory.
    """
    t_start = time.perf_counter()

    def emit(event: str, **fields: Any) -> None:
        sink({"event": event, "t": round(time.perf_counter() - t_start, 3), **fields})

    try:
        run_dir = resolve_run_dir(source, out, run_dir)
    except StageError as e:
        shown = str(run_dir if run_dir is not None else out)
        emit("run_start", source=str(source), duration=None, run_dir=shown, stages=[])
        emit("run_end", status="failed", run_dir=shown, wall_seconds=round(time.perf_counter() - t_start, 3),
             error=str(e), error_kind=None, completeness=None, warnings=[])
        raise
    ctx = Context(config=cfg, source=source.resolve(), run_dir=run_dir, emit=emit)
    fingerprint: str | None = None
    started = False
    keys: dict[str, str] = {}
    digests: dict[str, str] = {}
    records: list[StageRecord] = []
    skipped_records: list[StageRecord] = []
    current = ""
    t0 = time.perf_counter()
    try:
        if not source.is_file():
            raise StageError(f"video not found: {source}")
        plan = build_plan(cfg)
        skipped_records = plan.skipped
        forced = forced_stages(plan, force)
        check_run_folders(run_dir)  # before anything is written: no symbolic link, no folder of another user
        ctx.work_dir.mkdir(parents=True, exist_ok=True)
        check_run_folders(run_dir)
        store = ArtifactStore(ctx.work_dir, log=ctx.log)
        fingerprint = source_fingerprint(source)
        skipped = {s.stage: s.reason for s in plan.skipped}
        order = sorted([s.name for s in plan.stages] + list(skipped), key=STAGE_ORDER.index)
        duration = probe(ctx.source).duration
        started = True
        emit("run_start", source=str(ctx.source), duration=duration, run_dir=str(run_dir),
             stages=[{"name": n, "skipped_reason": skipped.get(n)} for n in order])
        for rec in plan.skipped:
            emit("stage_end", stage=rec.stage, status="skipped", wall_seconds=0.0, reason=rec.reason)

        for stage in plan.stages:
            current = ctx.stage = stage.name
            key = stage_key(
                fingerprint=fingerprint,
                stage=stage.name,
                version=stage.version,
                config={f: getattr(cfg, f) for f in stage.config_fields},
                dep_keys={d: keys[d] for d in stage.deps},
                dep_digests={d: digests[d] for d in stage.deps},
                extra=stage.key_extra(cfg) if stage.key_extra else "",
            )
            keys[stage.name] = key
            emit("stage_start", stage=stage.name)
            t0 = time.perf_counter()
            artifact = None if stage.name in forced else store.load(stage.filename, key, stage.model)
            if artifact is not None:
                status = "hit"
            else:
                status = "forced" if stage.name in forced else "miss"
                store.invalidate(stage.filename)  # until the new result is saved, the old one must not read as a hit
                ctx.forced = stage.name in forced
                ctx.metrics = {}
                artifact = stage.run(ctx)
                store.save(stage.filename, key, artifact, stage.outputs(artifact))
            digests[stage.name] = store.digest(stage.filename)
            ctx.artifacts[stage.name] = artifact
            wall = time.perf_counter() - t0
            emit("stage_end", stage=stage.name, status=EVENT_STATUS[status], wall_seconds=round(wall, 3),
                 summary=stage.summary(artifact))
            records.append(
                StageRecord(stage=stage.name, status=status, wall_seconds=wall, cache_key=key, version=stage.version,
                            metrics=dict(ctx.metrics) if status != "hit" else {})
            )
        current = ""
        write_transcript_md(run_dir / "transcript.md", ctx)
    except BaseException as e:
        interrupted = isinstance(e, KeyboardInterrupt)
        message = ("terminated" if isinstance(e, Terminated) else "interrupted") if interrupted else str(e)
        kind = getattr(e, "kind", None) if isinstance(e, StageError) else None  # machine-readable, for hosts
        if not started:
            emit("run_start", source=str(source), duration=None, run_dir=str(run_dir), stages=[])
        log = ctx.log_path if current and ctx.log_path.is_file() else None
        if current:
            wall = time.perf_counter() - t0
            emit("stage_end", stage=current, status="failed", wall_seconds=round(wall, 3), error=message,
                 error_kind=kind, log_path=str(log) if log else None)
            records.append(StageRecord(stage=current, status="failed", wall_seconds=wall, cache_key=keys.get(current),
                                       reason=message))
        total = time.perf_counter() - t_start
        emit("run_end", status="interrupted" if interrupted else "failed", run_dir=str(run_dir),
             wall_seconds=round(total, 3), error=message, error_kind=kind,
             completeness=_completeness_event(ctx, e.figures if isinstance(e, GateError) else None),
             warnings=_warnings(ctx))
        if fingerprint is not None:
            _write_run_json(run_dir, ctx, fingerprint, cfg, records + skipped_records, total,
                            "interrupted" if interrupted else "failed", message,
                            e.figures if isinstance(e, GateError) else None)
        raise

    total = time.perf_counter() - t_start
    record = _write_run_json(run_dir, ctx, fingerprint, cfg, records + skipped_records, total, "ok", None)
    emit("run_end", status="ok", run_dir=str(run_dir), wall_seconds=round(total, 3),
         outputs=[str(run_dir / p) for p in ("report.md", "timeline.json", "ANALYSIS_PROMPT.md", "transcript.md", "run.json")],
         completeness=_completeness_event(ctx), warnings=_warnings(ctx))
    return record, run_dir


def _completeness_event(ctx: Context, refused: dict | None = None) -> dict | None:
    """The completeness figures for `run_end`, as in run.json, plus `summary` (the line the `asr` stage shows): those of
    the transcript, or `refused`, the figures of a transcript the gate refused. None when there is no cross-checked
    transcript (an imported one, or a run that stopped before the `asr` stage)."""
    if refused is not None:
        c = Completeness.model_validate(refused)
    else:
        transcript = ctx.artifacts.get(asr.NAME)
        if not isinstance(transcript, TranscriptArtifact) or transcript.completeness is None:
            return None
        c = transcript.completeness
    return {**c.model_dump(exclude={"gaps"}), "summary": c.summary()}


def _warnings(ctx: Context) -> list[str]:
    """The warnings the run's artifacts carry, for `run_end`: layout detection, the speech backends (chunks kept
    without punctuation, chunks that stayed empty, cuts without a pause) and a transcript kept above the gate."""
    found: list[str] = []
    ingested = ctx.artifacts.get(ingest.STAGE.name)
    if isinstance(ingested, IngestArtifact) and ingested.layout.warning:
        found.append(ingested.layout.warning)
    found += [w for a in ctx.artifacts.values() if isinstance(a, AsrArtifact) for w in a.warnings]
    transcript = ctx.artifacts.get(asr.NAME)
    if isinstance(transcript, TranscriptArtifact) and transcript.completeness and transcript.completeness.warning:
        found.append(transcript.completeness.warning)
    return found


def _write_run_json(run_dir: Path, ctx: Context, fingerprint: str, cfg: Config, stages: list[StageRecord],
                    total: float, status: str, error: str | None, completeness: dict | None = None) -> RunRecord:
    """`completeness`: the figures of a transcript the gate refused (else they come from the `asr` artifact)."""
    transcript = ctx.artifacts.get(asr.NAME)
    if completeness is None and isinstance(transcript, TranscriptArtifact) and transcript.completeness is not None:
        completeness = transcript.completeness.model_dump(exclude={"gaps"})
    record = RunRecord(
        framescribe_version=__version__,
        source=str(ctx.source),
        source_fingerprint=fingerprint,
        config=cfg.to_dict(),
        stages=stages,
        total_wall_seconds=total,
        status=status,  # type: ignore[arg-type]
        error=error,
        completeness=completeness,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(run_dir / "run.json", record.model_dump_json(indent=1))
    return record
