"""Stage `ocr`: read the on-screen text of every key frame with positions, and diff it against the previous state.

Apple Vision "accurate" with language correction on a 2x Lanczos upscale; rectangles are in key-frame (content crop)
pixels. Key frames with identical bytes are recognised once.
"""

from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from .. import ocr_engine
from ..models import OcrArtifact, OcrLine, OcrState, VisualArtifact
from ..ocrdiff import diff_lines
from .base import Context, Stage, StageError

VERSION = 1
NAME = "ocr"


def run(ctx: Context) -> OcrArtifact:
    ocr_engine.require_macos()
    cfg = ctx.config
    if cfg.ocr_scale < 1:
        raise StageError("ocr_scale must be at least 1")
    visual = ctx.get("visual", VisualArtifact)
    languages = ocr_engine.vision_languages(cfg.language, ocr_engine.supported_languages())
    t0 = time.perf_counter()

    paths = {s.id: ctx.run_dir / s.keyframe_path for s in visual.states}
    for sid, path in paths.items():
        if not path.is_file():
            raise StageError(f"key frame of state {sid} is missing: {path}")
    hashes = {sid: hashlib.sha256(path.read_bytes()).hexdigest() for sid, path in paths.items()}

    first_of: dict[str, int] = {}
    for s in visual.states:
        first_of.setdefault(hashes[s.id], s.id)
    results = _recognise_all(ctx, {d: paths[sid] for d, sid in first_of.items()}, languages, first_of, len(visual.states))

    states: list[OcrState] = []
    prev_lines: list[OcrLine] | None = None
    prev_id: int | None = None
    crop = visual.content_rect
    for s in visual.states:
        digest = hashes[s.id]
        lines = results[digest]
        dup = None if first_of[digest] == s.id else first_of[digest]
        if prev_lines is None:
            added, removed, scroll = list(lines), [], None
        else:
            dy = s.change.scroll_dy if s.change.kind == "scroll" else None
            added, removed, scroll, _ = diff_lines(prev_lines, lines, crop.w, crop.h, dy)
        states.append(OcrState(
            state_id=s.id, keyframe_path=s.keyframe_path, content_hash=digest, duplicate_of=dup,
            diff_against=prev_id, lines=lines, added=added, removed=removed, scroll=scroll,
        ))
        prev_lines, prev_id = lines, s.id
    return OcrArtifact(
        engine=ocr_engine.ENGINE, scale=cfg.ocr_scale, level=ocr_engine.LEVEL, language_correction=True,
        languages=languages, states=states, frames_recognised=len(results), wall_seconds=time.perf_counter() - t0,
    )


def _recognise_all(ctx: Context, files: dict[str, Path], languages: list[str], first_of: dict[str, int],
                   n_states: int) -> dict[str, list[OcrLine]]:
    """Recognise each distinct key frame once, `ocr_workers` at a time (the result does not depend on the number)."""
    cfg = ctx.config

    def one(item: tuple[str, Path]) -> tuple[str, list[OcrLine]]:
        digest, path = item
        with Image.open(path) as im:
            return digest, ocr_engine.recognise(im, cfg.ocr_scale, languages)

    results: dict[str, list[OcrLine]] = {}
    total = len(files)
    ctx.progress(0, total, "frames", "starting")
    with ThreadPoolExecutor(max_workers=max(1, cfg.ocr_workers)) as pool:
        futures = [pool.submit(one, item) for item in files.items()]
        try:
            for done, fut in enumerate(as_completed(futures), start=1):
                digest, lines = fut.result()
                results[digest] = lines
                ctx.progress(done, total, "frames", f"state {first_of[digest]}/{n_states}: {len(lines)} lines")
        except BaseException:
            for f in futures:
                f.cancel()
            raise
    return results


def summary(a: OcrArtifact) -> str:
    n = len(a.states)
    lines = sum(len(s.lines) for s in a.states)
    return f"{n} states, {a.frames_recognised} frames read, {lines / n if n else 0:.0f} lines/state"


STAGE = Stage(
    name=NAME,
    version=VERSION,
    filename="ocr.json",
    model=OcrArtifact,
    run=run,
    config_fields=("ocr_scale", "language"),
    deps=("visual",),
    summary=summary,
)
