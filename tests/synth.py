"""Synthetic screen recordings for the visual and OCR tests: pages, scrolls, toasts, pointers, fades, noise."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
from conftest import FPS, encode_frames
from PIL import Image, ImageDraw, ImageFont

from framescribe.config import Config
from framescribe.ffmpeg import probe
from framescribe.models import IngestArtifact, Layout, Rect
from framescribe.stages import visual
from framescribe.stages.base import Context

W, H = 640, 360
WORDS = ("amber garden copper compass fennel forest grove harbor orchard juniper kettle meadow marble nettle oyster "
         "pinecone quill ribbon sorrel timber velvet willow walnut yarrow zinc").split()


def font(size: int):
    return ImageFont.load_default(size=size)


def words(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def page(seed: int, bg=(255, 255, 255), fg=(30, 30, 30), lines: int = 12, size: int = 14, h: int = H, pitch: int = 26,
         x: int = 30, top: int = 20, header: str | None = None) -> np.ndarray:
    """A page of text lines (random words from the seed); `header` draws a heading bar in the first rows."""
    rng = random.Random(seed)
    img = Image.new("RGB", (W, h), bg)
    d = ImageDraw.Draw(img)
    if header is not None:
        d.rectangle([0, 0, W, 34], fill=tuple(max(0, c - 70) for c in bg))
        d.text((x, 8), header, fill=fg, font=font(16))
    for i in range(lines):
        d.text((x, top + 30 + i * pitch), words(rng, rng.randint(3, 8)), fill=fg, font=font(size))
    return np.asarray(img)


def tall_document(seed: int, h: int = 1400) -> np.ndarray:
    return page(seed, lines=(h - 60) // 26, h=h, header=None, top=0)


ARROW = [(0, 0), (0, 17), (4, 13), (8, 20), (11, 19), (7, 12), (12, 12)]
ARROW_BOX = (12, 20)  # width, height of the sprite


def draw_arrow(frame: np.ndarray, x: int, y: int) -> None:
    img = Image.fromarray(frame)
    d = ImageDraw.Draw(img)
    pts = [(x + px, y + py) for px, py in ARROW]
    d.polygon(pts, fill=(0, 0, 0), outline=(255, 255, 255))
    frame[:] = np.asarray(img)


def encode(frames, dest: Path) -> Path:
    encode_frames(frames, W, H, dest, extra_inputs=[])
    return dest


def hold(img: np.ndarray, seconds: float):
    return [img] * int(round(seconds * FPS))


def crossfade(a: np.ndarray, b: np.ndarray, seconds: float):
    n = int(round(seconds * FPS))
    return [((a.astype(np.float32) * (1 - (i + 1) / n)) + b.astype(np.float32) * ((i + 1) / n)).astype(np.uint8) for i in range(n)]


def scroll_frames(doc: np.ndarray, y0: int, y1: int, frames: int, header: np.ndarray | None = None):
    """Viewport of the document sliding from row y0 to y1 in `frames` frames; an optional fixed header on top."""
    out = []
    for i in range(frames):
        y = round(y0 + (y1 - y0) * (i + 1) / frames)
        view = doc[y : y + H].copy()
        if header is not None:
            view[: header.shape[0]] = header
        out.append(view)
    return out


def context_for(video: Path, run_dir: Path, cfg: Config | None = None) -> Context:
    """A Context with a hand-made ingest artifact whose content rect is the whole frame."""
    info = probe(video)
    cfg = cfg or Config()
    ingest = IngestArtifact(
        source=str(video), duration=info.duration, video=info.video, audio=None, audio_wav=None, embedded_captions=[],
        layout=Layout(frame_w=info.video.width, frame_h=info.video.height,
                      content_rect=Rect(x=0, y=0, w=info.video.width, h=info.video.height), method="manual", confidence=1.0),
    )
    ctx = Context(config=cfg, source=video, run_dir=run_dir)
    ctx.artifacts["ingest"] = ingest
    return ctx


def analyse(video: Path, run_dir: Path, **config):
    ctx = context_for(video, run_dir, Config(**config))
    return visual.run(ctx), ctx
