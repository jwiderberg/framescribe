"""Stage `ingest`: probe the video, extract 16 kHz audio and embedded captions, detect the layout."""

from __future__ import annotations

from ..captions import parse_captions
from ..ffmpeg import extract_audio_wav, extract_subtitles_srt, probe
from ..fmt import timestamp
from ..layout import detect_layout, rect_from_option
from ..models import Caption, IngestArtifact, Layout
from .base import Context, Stage, StageError

VERSION = 3
AUDIO_FILE = "audio.wav"


def run(ctx: Context) -> IngestArtifact:
    info = probe(ctx.source)
    audio_wav = None
    if info.audio is not None:
        ctx.progress(0, info.duration, "audio_s", "extracting audio")
        extract_audio_wav(
            ctx.source,
            ctx.work_dir / AUDIO_FILE,
            lambda seconds: ctx.progress(seconds, info.duration, "audio_s", "extracting audio"),
        )
        audio_wav = f"work/{AUDIO_FILE}"
    else:
        ctx.log("no audio stream: only a transcript supplied with --transcript can be used")
    ctx.progress(0, 1, "step", "reading embedded captions")
    try:
        captions = parse_captions(extract_subtitles_srt(ctx.source)) if info.subtitle_streams else []
    except (ValueError, OverflowError) as e:  # a ValidationError too: times out of range
        raise StageError(f"the video's embedded captions cannot be read: {str(e)[:300]}") from None
    layout = _layout(ctx, info)
    if layout.warning:
        ctx.log(layout.warning)
    return IngestArtifact(
        source=str(ctx.source),
        duration=info.duration,
        video=info.video,
        audio=info.audio,
        audio_wav=audio_wav,
        embedded_captions=within_duration(captions, info.duration),
        layout=layout,
    )


def within_duration(captions: list[Caption], duration: float) -> list[Caption]:
    """Drop captions that start at or after the end of the video and cut the last ones off at it."""
    return [c.model_copy(update={"end": min(c.end, duration)}) for c in captions if c.start < duration]


def _layout(ctx: Context, info) -> Layout:
    override = ctx.config.content_rect
    if override is None:
        return detect_layout(
            ctx.source,
            info,
            ctx.config.layout_samples,
            lambda done, total: ctx.progress(done, total, "frames", "sampling frames for layout"),
        )
    rect = rect_from_option(override)
    frame_w, frame_h = info.video.width, info.video.height
    if rect.w <= 0 or rect.h <= 0 or rect.x < 0 or rect.y < 0 or rect.x1 > frame_w or rect.y1 > frame_h:
        raise StageError(f"--content-rect {tuple(override)} does not fit inside the {frame_w}x{frame_h} frame")
    return Layout(
        frame_w=frame_w,
        frame_h=frame_h,
        content_rect=rect,
        method="manual",
        confidence=1.0,
    )


def summary(a: IngestArtifact) -> str:
    c = a.layout.content_rect
    return (
        f"{timestamp(a.duration)}, content {c.x},{c.y} {c.w}x{c.h} + {len(a.layout.other_rects)} other region(s), "
        f"{len(a.embedded_captions)} captions"
    )


STAGE = Stage(
    name="ingest",
    version=VERSION,
    filename="ingest.json",
    model=IngestArtifact,
    run=run,
    config_fields=("content_rect", "layout_samples"),
    outputs=lambda a: [AUDIO_FILE] if a.audio_wav else [],
    summary=summary,
)
