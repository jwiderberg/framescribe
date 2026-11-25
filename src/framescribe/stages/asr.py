"""Stage `asr`: the final transcript. The primary backend's words, cross-checked against every other source of the
same speech (the second parakeet pass, another backend, embedded captions); phrases they have and the primary lacks
are filled in and marked (see completeness.py). Then speakers are assigned."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path

from ..chunking import Levels, read_wav
from ..completeness import Source, caption_source, above_limit_warning, complete, gate
from ..config import Config
from ..models import (
    AsrArtifact,
    Caption,
    IngestArtifact,
    TranscriptArtifact,
    TranscriptSegment,
    TranscriptWord,
)
from ..words import segment_sentences
from .asr_import import import_transcript
from ..errors import GateError
from .base import Context, Stage

VERSION = 7  # 7: a share above the limit is kept with a warning (`above_limit`, `limit`) unless --strict-completeness
NEAREST_CAPTION_SECONDS = 4.0
NAME = "asr"
FILENAME = "transcript.json"
GATE_FAILED = "transcript.gate-failed.json"  # under work/: the transcript the gate refused, for diagnosis
MARKS = {True: ("[+ ", " +]"), False: ("[? ", " ?]")}  # filled words: confirmed, unconfirmed


def assign_speaker(start: float, end: float, captions: Sequence[Caption]) -> str | None:
    """The named speaker whose captions overlap [start, end] the longest.

    Captions are coarse (4 s buckets with gaps), so a segment that falls between two buckets takes the
    speaker of the nearest named caption within NEAREST_CAPTION_SECONDS. None if there is no such caption.
    """
    overlap: dict[str, float] = defaultdict(float)
    nearest: tuple[float, str] | None = None
    for c in captions:
        if c.speaker is None:
            continue
        overlap[c.speaker] += max(0.0, min(end, c.end) - max(start, c.start))
        distance = max(0.0, c.start - end, start - c.end)
        if nearest is None or distance < nearest[0]:
            nearest = (distance, c.speaker)
    best = max(overlap.items(), key=lambda kv: kv[1], default=None)
    if best and best[1] > 0:
        return best[0]
    return nearest[1] if nearest and nearest[0] <= NEAREST_CAPTION_SECONDS else None


def marked_text(words: Sequence[TranscriptWord]) -> str:
    """The words joined by spaces, each run of filled words in its mark: [+ confirmed +] or [? unconfirmed ?]."""
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if not w.filled:
            out.append(w.text)
            i += 1
            continue
        j = i
        while j < len(words) and words[j].filled and bool(words[j].confirmed) == bool(w.confirmed):
            j += 1
        left, right = MARKS[bool(w.confirmed)]
        out.append(left + " ".join(x.text for x in words[i:j]) + right)
        i = j
    return " ".join(out)


def build_transcript(primary: AsrArtifact, others: Sequence[AsrArtifact], captions: Sequence[Caption] = (),
                     speech_share: Callable[[float, float], float] | None = None,
                     progress: Callable[[int, int, str], None] = lambda done, total, note: None,
                     unavailable: Sequence[str] = ()) -> TranscriptArtifact:
    """The primary's words with the gaps that the other sources show filled in, as sentences."""
    sources: list[Source] = []
    if primary.second_pass is not None:
        sources.append(Source(primary.second_pass.name, primary.second_pass.words))
    sources += [Source(a.backend, a.words) for a in others]
    if captions:
        sources.append(caption_source(captions))
    result = complete(primary.words, primary.backend, sources, speech_share, progress, unavailable)
    words = result.words
    segments = [
        TranscriptSegment(id=i, start=s.start, end=s.end, text=marked_text(words[s.word_start_idx : s.word_end_idx]),
                          words=words[s.word_start_idx : s.word_end_idx])
        for i, s in enumerate(segment_sentences(words), start=1)
    ]
    return TranscriptArtifact(
        primary=primary.backend,
        segments=segments,
        alternates={a.backend: a.segments for a in others},
        sources=[a.backend for a in (primary, *others)],
        completeness=result.completeness,
    )


def speech_share_of(wav: Path) -> Callable[[float, float], float]:
    """share(a, b): the share of the 10 ms frames in [a, b] whose level is above the pause threshold. The audio is
    read on the first call."""
    levels: list[Levels] = []

    def share(a: float, b: float) -> float:
        if not levels:
            levels.append(Levels(*read_wav(wav)))
        return levels[0].speech_share(a, b)

    return share


def run(ctx: Context, backends: tuple[str, ...]) -> TranscriptArtifact:
    cfg = ctx.config
    ingest = ctx.get("ingest", IngestArtifact)
    if cfg.transcript is not None:
        transcript = import_transcript(cfg.transcript)
    else:
        arts = [ctx.get(b, AsrArtifact) for b in backends]
        unavailable = []
        if not any(a.backend == "whisperkit" for a in arts):
            unavailable.append("whisperkit: not part of this run")
        if not ingest.embedded_captions:
            unavailable.append("captions: the video has no embedded captions")
        if arts[0].second_pass is None:
            unavailable.append(f"second pass: {arts[0].backend} makes none")
        share = speech_share_of(ctx.run_dir / ingest.audio_wav) if ingest.audio_wav else None
        transcript = build_transcript(
            arts[0], arts[1:], ingest.embedded_captions, share,
            progress=lambda done, total, note: ctx.progress(done, total, "sources", note), unavailable=unavailable)
        c = transcript.completeness
        assert c is not None
        for g in c.gaps:
            if g.status == "rejected":
                ctx.log(f"not filled: {g.start:.2f}-{g.end:.2f} s from {g.chosen}: {g.reason}")
        failed = ctx.work_dir / GATE_FAILED
        c.limit = cfg.max_filled_share
        problem = gate(c, cfg.max_filled_share, where=f"work/{GATE_FAILED}")
        if problem is not None and cfg.strict_completeness:
            # not the stage's artifact (that is only written on success, so a failed stage never reads as cached):
            # the refused transcript with its figures and gaps, for diagnosis
            failed.parent.mkdir(parents=True, exist_ok=True)
            failed.write_text(transcript.model_dump_json(indent=1))
            raise GateError(problem, c.model_dump(exclude={"gaps"}))
        failed.unlink(missing_ok=True)
        if (warning := above_limit_warning(c, cfg.max_filled_share)) is not None:
            c.above_limit = True
            c.warning = warning
            ctx.log(c.warning)
    for seg in transcript.segments:
        if seg.speaker is None:
            seg.speaker = assign_speaker(seg.start, seg.end, ingest.embedded_captions)
    return transcript


def transcript_hash(cfg: Config) -> str:
    return hashlib.sha256(Path(cfg.transcript).read_bytes()).hexdigest() if cfg.transcript else ""


def describe(t: TranscriptArtifact) -> str:
    speakers = sorted({s.speaker for s in t.segments if s.speaker})
    text = f"{len(t.segments)} segments from {t.primary}, speakers: {', '.join(speakers) or 'none'}"
    if t.completeness is not None:
        text += "; " + t.completeness.summary()
    else:
        text += "; imported transcript, taken as given: not cross-checked"
    return text


def make_stage(backend_stages: tuple[str, ...]) -> Stage:
    """The `asr` stage depending on the given backend stages (none when a transcript is imported)."""
    return Stage(
        name=NAME,
        version=VERSION,
        filename=FILENAME,
        model=TranscriptArtifact,
        run=lambda ctx: run(ctx, backend_stages),
        config_fields=("transcript", "max_filled_share", "strict_completeness"),
        deps=("ingest", *backend_stages),
        key_extra=transcript_hash,
        summary=describe,
    )
