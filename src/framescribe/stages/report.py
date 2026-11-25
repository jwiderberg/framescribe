"""Stage `report`: the evidence document `report.md`, `timeline.json` and `ANALYSIS_PROMPT.md`.

Deterministic: no model is called and nothing is interpreted. The cached artifact is the timeline (`work/timeline.json`);
the three files in the run directory are side files, so deleting or truncating one makes the stage run again.
"""

from __future__ import annotations

from .. import __version__
from ..cache import write_if_changed
from ..models import AsrArtifact, IngestArtifact, OcrArtifact, TimelineArtifact, TranscriptArtifact, VisualArtifact
from ..report_md import PROMPT, render_report
from ..steps import build_steps, find_chrome
from ..timeline import build_timeline
from .base import Context, Stage, StageError

VERSION = 5  # 5: the Completeness row leads with a share above the usual level
NAME = "report"
SIDE_FILES = ("../report.md", "../timeline.json", "../ANALYSIS_PROMPT.md")


def run(ctx: Context) -> TimelineArtifact:
    cfg = ctx.config
    if cfg.step_min_seconds <= 0 or cfg.step_max_seconds < 2 * cfg.step_min_seconds or cfg.step_extra_frames < 0:
        raise StageError("step_min_seconds must be positive, step_max_seconds at least twice that, step_extra_frames not negative")
    ingest = ctx.get("ingest", IngestArtifact)
    transcript = ctx.get("asr", TranscriptArtifact)
    visual = ctx.get("visual", VisualArtifact)
    ocr = ctx.get("ocr", OcrArtifact)
    if not visual.states:
        raise StageError("the visual stage found no states; there is nothing to report on")
    if {s.id for s in visual.states} != {s.state_id for s in ocr.states}:
        raise StageError("the visual and ocr artifacts do not describe the same states")
    for s in visual.states:
        if not (ctx.run_dir / s.keyframe_path).is_file():
            raise StageError(f"key frame of state {s.id} is missing: {ctx.run_dir / s.keyframe_path}")

    ctx.progress(0, 1, "steps", "choosing steps")
    chrome = find_chrome(ocr)
    plans = build_steps(visual, transcript, ocr, chrome, cfg.step_min_seconds, cfg.step_max_seconds, cfg.step_extra_frames)
    timeline = build_timeline(
        ingest.source, transcript.primary, visual, transcript, ocr, plans, chrome, cfg.step_min_seconds, cfg.step_max_seconds,
        on_step=lambda done, total: ctx.progress(done, total, "steps", f"timeline of step {done}/{total}"),
    )
    ctx.progress(len(plans), len(plans), "steps", "writing report.md")
    # what the speech stage warned about, read from its artifact(s) in this run (a transcript import has none)
    speech_warnings = [w for a in ctx.artifacts.values() if isinstance(a, AsrArtifact) for w in a.warnings]
    write_if_changed(ctx.run_dir / "report.md", render_report(
        timeline, visual, ocr, transcript, __version__, cfg.max_filled_share, speech_warnings))
    write_if_changed(ctx.run_dir / "timeline.json", timeline.model_dump_json(indent=1) + "\n")
    write_if_changed(ctx.run_dir / "ANALYSIS_PROMPT.md", PROMPT)
    return timeline


def summary(t: TimelineArtifact) -> str:
    pointers = sum(1 for s in t.steps for e in s.events if e.type == "pointer")
    return f"{len(t.steps)} steps, {pointers} pointer hints"


STAGE = Stage(
    name=NAME,
    version=VERSION,
    filename="timeline.json",
    model=TimelineArtifact,
    run=run,
    config_fields=("step_min_seconds", "step_max_seconds", "step_extra_frames"),
    deps=("asr", "visual", "ocr"),
    outputs=lambda artifact: SIDE_FILES,
    summary=summary,
)
