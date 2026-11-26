"""Backend `import`: a transcript supplied from outside (SRT, VTT, or this project's transcript JSON)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ..captions import parse_captions
from ..models import TranscriptArtifact, TranscriptSegment
from .base import StageError


MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024  # far more than the transcript of 12 hours of speech


def import_transcript(path: Path) -> TranscriptArtifact:
    """Cues become segments without word timings. A transcript JSON must validate as framescribe's own (its source
    and reason names among them, models.NAME_PATTERN); its segments, speakers and word timings are kept, but it is not
    this run's cross-check: its completeness figures are dropped and its words lose their fill flags, so it is shown
    like any imported transcript, without the tool's marks (see from_json)."""
    if not path.is_file():
        raise StageError(f"transcript file not found: {path}")
    suffix = path.suffix.lower()
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise StageError(f"{path} is larger than {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB; that is not a transcript")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise StageError(f"{path} is not UTF-8 text ({e.reason} at byte {e.start})") from None
    if suffix == ".json":
        try:
            return from_json(TranscriptArtifact.model_validate_json(text))
        except ValidationError as e:  # NaN, infinite or negative times and unknown names included
            first = e.errors()[0] if e.errors() else {"loc": (), "msg": str(e)}
            where = ".".join(str(x) for x in first["loc"]) or "the file"
            raise StageError(f"{path} is not a framescribe transcript JSON ({e.error_count()} error(s)); first, at "
                             f"{where}: {str(first['msg'])[:300]}") from None
    if suffix not in (".srt", ".vtt"):
        raise StageError(f"unsupported transcript format {suffix!r}; use .srt, .vtt or .json")
    try:
        cues = parse_captions(text)
    except (ValueError, OverflowError) as e:  # a ValidationError too: times out of range
        raise StageError(f"{path}: {str(e)[:300]}") from None
    if not cues:
        raise StageError(f"{path} contains no cues")
    segments = [
        TranscriptSegment(id=i, start=c.start, end=c.end, text=c.text, speaker=c.speaker)
        for i, c in enumerate(cues, start=1)
    ]
    return TranscriptArtifact(primary="import", segments=segments, sources=[f"import:{path.name}"])


def from_json(t: TranscriptArtifact) -> TranscriptArtifact:
    """An imported transcript JSON as an imported transcript: no completeness figures, and no word marked as filled
    in, confirmed or taken from a source. Which words another run filled in is that file's claim, which this run did
    not check; the marks in the segments' text stay as recorded text (escaped in Markdown, never the tool's marks)."""
    segments = [seg.model_copy(update={"words": [w.model_copy(update={"filled": False, "confirmed": None, "source": None})
                                                 for w in seg.words]}) for seg in t.segments]
    return t.model_copy(update={"segments": segments, "completeness": None})
