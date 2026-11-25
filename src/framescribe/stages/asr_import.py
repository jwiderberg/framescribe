"""Backend `import`: a transcript supplied from outside (SRT, VTT, or this project's transcript JSON)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ..captions import parse_captions
from ..models import TranscriptArtifact, TranscriptSegment
from .base import StageError


MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024  # far more than the transcript of 12 hours of speech


def import_transcript(path: Path) -> TranscriptArtifact:
    """Cues become segments without word timings; a transcript JSON is used as is."""
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
            return TranscriptArtifact.model_validate_json(text)
        except ValidationError as e:  # NaN, infinite or negative times included
            raise StageError(f"{path} is not a framescribe transcript JSON: {str(e)[:600]}") from None
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
