"""Run configuration. Each stage declares which of these fields it uses (see pipeline.Stage)."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

AsrMode = Literal["parakeet", "whisperkit", "both", "auto"]

ENV_WHISPERKIT_CLI = "FRAMESCRIBE_WHISPERKIT_CLI"
ENV_WHISPERKIT_MODEL = "FRAMESCRIBE_WHISPERKIT_MODEL"


@dataclass(frozen=True)
class Config:
    asr: AsrMode = "auto"
    transcript: Path | None = None
    content_rect: tuple[int, int, int, int] | None = None
    layout_samples: int = 40
    parakeet_model: str = "mlx-community/parakeet-tdt-0.6b-v3"
    chunk_seconds: float = 30.0  # target chunk length for parakeet (measured, docs/MEASUREMENTS.md)
    cut_search_seconds: float = 5.0
    chunk_max_seconds: float = 40.0
    # MLX keeps freed buffers in a cache that is unbounded by default and grew to tens of GB on a long recording; the
    # parakeet stage bounds it to this many MB (0 turns the cache off). Memory only, the words do not depend on it,
    # so it is not part of the cache key.
    mlx_cache_limit_mb: int = 512
    whisperkit_cli: str | None = None
    whisperkit_model: str | None = None
    language: str = "en"
    # asr stage: completeness gate
    max_filled_share: float = 0.03  # more of the transcript filled in from other sources than this is warned about ...
    strict_completeness: bool = False  # ... or, with this, fails the stage
    # visual stage (defaults measured in docs/MEASUREMENTS.md)
    visual_fps: float = 2.0
    visual_open_fraction: float = 0.01
    visual_settle_fraction: float = 0.002
    visual_merge_seconds: float = 1.0
    visual_keep_fraction: float = 0.01
    visual_min_state_seconds: float = 1.5
    visual_max_states_per_minute: int = 20
    visual_max_event_seconds: float = 10.0
    keyframe_quality: int = 90
    # ocr stage
    ocr_scale: int = 2
    ocr_workers: int = 3  # parallel Vision requests; the result does not depend on it, so it is not part of the cache key
    # report stage
    step_min_seconds: float = 30.0
    step_max_seconds: float = 240.0
    step_extra_frames: int = 3

    def with_env_defaults(self) -> Config:
        """Fill the WhisperKit paths from the environment where the options were not given."""
        cli = self.whisperkit_cli or os.environ.get(ENV_WHISPERKIT_CLI) or None
        model = self.whisperkit_model or os.environ.get(ENV_WHISPERKIT_MODEL) or None
        return replace(self, whisperkit_cli=cli, whisperkit_model=model)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["transcript"] = str(self.transcript) if self.transcript else None
        return d
