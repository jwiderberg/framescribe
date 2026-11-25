"""Backend `import`: a transcript supplied from outside (SRT, VTT, or this project's transcript JSON)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ..captions import parse_captions
from ..models import TranscriptArtifact, TranscriptSegment
from .base import StageError


def import_transcript(path: Path) -> TranscriptArtifact:
    """Cues become segments without word timings; a transcript JSON is used as is."""
    if not path.is_file():
        raise StageError(f"transcript file not found: {path}")
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8-sig")
    if suffix == ".json":
        try:
            return TranscriptArtifact.model_validate_json(text)
        except ValidationError as e:
            raise StageError(f"{path} is not a framescribe transcript JSON: {e}") from e
    if suffix not in (".srt", ".vtt"):
        raise StageError(f"unsupported transcript format {suffix!r}; use .srt, .vtt or .json")
    try:
        cues = parse_captions(text)
    except ValueError as e:
        raise StageError(f"{path}: {e}") from e
    if not cues:
        raise StageError(f"{path} contains no cues")
    segments = [
        TranscriptSegment(id=i, start=c.start, end=c.end, text=c.text, speaker=c.speaker)
        for i, c in enumerate(cues, start=1)
    ]
    return TranscriptArtifact(primary="import", segments=segments, sources=[f"import:{path.name}"])
