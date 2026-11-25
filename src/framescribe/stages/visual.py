"""Stage `visual`: find the distinct visual states of the shared screen, save one settled key frame per state.

Pass 1 decodes the content crop as gray at `visual_fps` through an ffmpeg pipe and runs change detection and
pointer tracking on the stream (see changedet.py and cursor.py). Pass 2 decodes only the chosen settled samples in
colour and writes them as JPEG. Peak memory is a few frames plus the artifact.
"""

from __future__ import annotations

import math
import time
from collections import Counter

import cv2
import numpy as np
from PIL import Image

from ..changedet import DetectParams, StateDetector, signature
from ..cursor import CursorTracker
from ..errors import StageError
from ..fmt import timestamp
from ..framestream import gray_frames, rgb_frames
from ..models import CapInfo, Change, CursorSample, IngestArtifact, VisualArtifact, VisualState
from ..safeio import atomic_output
from .base import Context, Stage

VERSION = 3
NAME = "visual"
FRAMES_DIR = "frames"
PROGRESS_EVERY = 30  # samples between progress reports
SIGNATURE_TOLERANCE = 12.0  # mean abs difference of the 32x16 gray thumbnails between the two passes


def params_from(cfg) -> DetectParams:
    return DetectParams(
        fps=cfg.visual_fps,
        open_fraction=cfg.visual_open_fraction,
        settle_fraction=cfg.visual_settle_fraction,
        merge_seconds=cfg.visual_merge_seconds,
        keep_fraction=cfg.visual_keep_fraction,
        min_state_seconds=cfg.visual_min_state_seconds,
        max_states_per_minute=cfg.visual_max_states_per_minute,
        max_event_seconds=cfg.visual_max_event_seconds,
    )


def keyframe_name(state_id: int, start: float) -> str:
    h, m, s = int(start) // 3600, int(start) % 3600 // 60, int(start) % 60
    return f"s{state_id:04d}_t{h:02d}-{m:02d}-{s:02d}.jpg"


def run(ctx: Context) -> VisualArtifact:
    cfg = ctx.config
    ingest = ctx.get("ingest", IngestArtifact)
    rect, duration = ingest.layout.content_rect, ingest.duration
    if cfg.visual_fps <= 0 or cfg.visual_max_states_per_minute < 1:
        raise StageError("visual_fps must be positive and visual_max_states_per_minute at least 1")
    params = params_from(cfg)
    t0 = time.perf_counter()

    detector = StateDetector(params)
    tracker = CursorTracker((rect.h, rect.w))
    expected = max(1, math.ceil(duration * params.fps))
    prev = None
    for k, frame in enumerate(gray_frames(ctx.source, rect, params.fps)):
        detector.feed(frame)
        if prev is not None:
            tracker.feed(k / params.fps, prev, frame, detector.last_pair_fraction)
        prev = frame
        if k % PROGRESS_EVERY == 0:
            ctx.progress(min(k, expected), expected, "frames",
                         f"change detection at {timestamp(k / params.fps)}, {len(detector.states)} states")
    detector.finish()
    if prev is None:
        raise StageError(f"ffmpeg decoded no frames from {ctx.source}")
    ctx.progress(expected, expected, "frames", f"change detection done, {len(detector.states)} states")
    for note in _cap_notes(detector):
        ctx.log(note)

    found = [s for s in detector.states if s.start < duration]
    starts = [s.start for s in found]
    ends = [*starts[1:], duration]
    names = [keyframe_name(i, s.start) for i, s in enumerate(found, start=1)]
    _write_keyframes(ctx, rect, params, found, names)

    samples = tracker.finish()
    states: list[VisualState] = []
    for i, (s, end, name) in enumerate(zip(found, ends, names), start=1):
        c = s.change
        states.append(VisualState(
            id=i, start=s.start, end=end, keyframe_time=min(s.settled_time, duration), keyframe_path=f"{FRAMES_DIR}/{name}",
            settled=s.settled,
            change=Change(kind=c.kind, kind_confidence=c.kind_confidence, changed_fraction=round(c.changed_fraction, 5),  # type: ignore[arg-type]
                          changed_rect=c.changed_rect, scroll_dy=c.scroll_dy),
            cursor=[CursorSample(t=round(x.t, 3), x=round(x.x, 1), y=round(x.y, 1), confidence=round(x.confidence, 2))
                    for x in samples if s.start <= x.t < end],
        ))
    cap = detector.cap
    return VisualArtifact(
        fps_sampled=params.fps,
        duration=duration,
        content_rect=rect,
        states=states,
        cap=CapInfo(max_states_per_minute=params.max_states_per_minute, min_state_seconds=params.min_state_seconds,
                    max_event_seconds=params.max_event_seconds, hit=cap.hit, forced_closes=cap.forced_closes,
                    suppressed_changes=cap.suppressed),
        cursor_samples=len(samples),
        wall_seconds=time.perf_counter() - t0,
    )


def _cap_notes(detector: StateDetector) -> list[str]:
    cap = detector.cap
    notes = []
    if cap.forced_closes:
        notes.append(f"{cap.forced_closes} change(s) did not settle within {detector.p.max_event_seconds:g} s (an animation, "
                     "video or camera feed on screen) and were cut off at their calmest frame")
    if cap.suppressed:
        notes.append(f"{cap.suppressed} change(s) dropped by the state-rate safeguard "
                     f"(min {detector.p.min_state_seconds:g} s between states, max {detector.p.max_states_per_minute} per minute)")
    return notes


def _write_keyframes(ctx: Context, rect, params: DetectParams, found, names: list[str]) -> None:
    frames_dir = ctx.run_dir / FRAMES_DIR
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("s????_t??-??-??.jpg"):
        old.unlink()  # from an earlier run with other states
    by_index = {s.settled_index: (n, s) for n, s in zip(names, found)}
    total = len(found)
    ctx.progress(0, total, "key frames", "extracting key frames")
    done = 0
    for index, rgb in rgb_frames(ctx.source, rect, params.fps, by_index):
        name, state = by_index[index]
        _check_same_frame(rgb, state.signature, name)
        # a new file with an unpredictable name, renamed over the key frame: a link planted in frames/ is replaced,
        # never written through
        with atomic_output(frames_dir / name, suffix=".part") as f:
            Image.fromarray(rgb).save(f, format="JPEG", quality=ctx.config.keyframe_quality, subsampling=0)
        done += 1
        ctx.progress(done, total, "key frames", f"saving key frame {done}/{total}")


def _check_same_frame(rgb: np.ndarray, sig: np.ndarray, name: str) -> None:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    diff = float(np.abs(signature(gray).astype(np.float32) - sig.astype(np.float32)).mean())
    if diff > SIGNATURE_TOLERANCE:
        raise StageError(f"key frame {name} does not match the frame that change detection settled on "
                         f"(thumbnail difference {diff:.1f}); the two decoding passes disagree")


def summary(a: VisualArtifact) -> str:
    kinds = Counter(s.change.kind for s in a.states)
    per_min = len(a.states) / (a.duration / 60) if a.duration else 0.0
    kinds_text = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    cap = ", state-rate safeguard hit" if a.cap.hit else ""
    return f"{len(a.states)} states ({per_min:.1f}/min; {kinds_text}), {a.cursor_samples} pointer samples{cap}"


STAGE = Stage(
    name=NAME,
    version=VERSION,
    filename="visual.json",
    model=VisualArtifact,
    run=run,
    config_fields=(
        "visual_fps", "visual_open_fraction", "visual_settle_fraction", "visual_merge_seconds", "visual_keep_fraction",
        "visual_min_state_seconds", "visual_max_states_per_minute", "visual_max_event_seconds", "keyframe_quality",
    ),
    deps=("ingest",),
    outputs=lambda a: [f"../{s.keyframe_path}" for s in a.states],  # relative to work/
    summary=summary,
)
