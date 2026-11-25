"""Plain Markdown rendering of the transcript, `transcript.md`."""

from __future__ import annotations

from pathlib import Path

from .cache import write_if_changed
from .completeness import CUE_TOLERANCE, TIME_TOLERANCE
from .fmt import timestamp
from .models import Completeness, IngestArtifact, TranscriptArtifact
from .stages.base import Context

FILLED_NOTE = ("Words in [+ ... +] or [? ... ?] were not in the primary speech recognition and were filled in from "
               "other transcripts of the same audio: [+ ... +] when at least two of them have the gap and every one of those "
               f"words is in at least two of them within {TIME_TOLERANCE:g} s ({CUE_TOLERANCE:g} s of a caption) of that point, [? ... ?] "
               "otherwise.")


def above_limit_note(c: Completeness) -> str | None:
    """Plain words for a transcript whose filled share is above the completeness limit, else None: how many words and
    what share were filled in, that it is above the usual level and that the filled-in words should be checked."""
    if not c.above_limit or c.warning is None:
        return None
    return "ABOVE THE USUAL LEVEL: " + c.warning


def render_transcript(ingest: IngestArtifact, transcript: TranscriptArtifact, max_filled_share: float | None = None) -> str:
    lines = [
        f"# Transcript: {Path(ingest.source).name}",
        "",
        f"Duration {timestamp(ingest.duration)}. Source of text: {transcript.primary}.",
        "",
    ]
    c = transcript.completeness
    if c is not None:
        above = above_limit_note(c)
        if above:
            lines += [f"**Completeness: {above}.**", ""]
        lines += [f"Completeness: {c.primary_words} words from {c.primary}; {c.summary()}.", ""]
        if c.filled_words:
            lines += [FILLED_NOTE, ""]
    else:
        lines += ["Completeness: imported transcript, taken as given: not cross-checked.", ""]
    for seg in transcript.segments:
        who = f" {seg.speaker}:" if seg.speaker else ""
        lines.append(f"**[{timestamp(seg.start)}]**{who} {seg.text}")
        lines.append("")
    return "\n".join(lines)


def write_transcript_md(path: Path, ctx: Context) -> bool:
    """Write transcript.md, only when its content changed; True when it was written."""
    ingest = ctx.get("ingest", IngestArtifact)
    transcript = ctx.get("asr", TranscriptArtifact)
    return write_if_changed(path, render_transcript(ingest, transcript, ctx.config.max_filled_share))
