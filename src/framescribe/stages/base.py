"""What every stage is made of: the Stage description and the Context it runs in."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from ..config import Config
from ..errors import StageError
from ..models import Artifact

__all__ = ["Context", "Stage", "StageError"]

A = TypeVar("A", bound=Artifact)


@dataclass
class Context:
    config: Config
    source: Path
    run_dir: Path
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    emit: Callable[..., None] = lambda event, **fields: None  # set by the runner
    stage: str = ""  # the running stage, set by the runner
    forced: bool = False  # the running stage is recomputed because of --force, set by the runner
    metrics: dict[str, float | int] = field(default_factory=dict)  # what a stage measured about its own run: goes to run.json, not the artifact

    @property
    def work_dir(self) -> Path:
        return self.run_dir / "work"

    @property
    def log_path(self) -> Path:
        return self.work_dir / f"{self.stage}.log"

    def progress(self, done: float, total: float, unit: str, note: str | None = None) -> None:
        """Report real progress. `unit` "audio_s" means seconds of audio; the display derives realtime speed."""
        self.emit("stage_progress", stage=self.stage, done=float(done), total=float(total), unit=unit, note=note)

    def log(self, message: str) -> None:
        """Record a message in the stage log file and in the event stream. Stages never print."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(message.rstrip("\n") + "\n")
        self.emit("log", stage=self.stage, message=message)

    def get(self, stage: str, model: type[A]) -> A:
        """An upstream artifact. Stages read earlier results only through this."""
        artifact = self.artifacts[stage]
        assert isinstance(artifact, model)
        return artifact


@dataclass(frozen=True)
class Stage:
    name: str
    version: int
    filename: str  # artifact file under work/
    model: type[Artifact]
    run: Callable[[Context], Artifact]
    config_fields: tuple[str, ...]  # the Config fields this stage reads; they enter the cache key
    deps: tuple[str, ...] = ()
    outputs: Callable[[Any], Sequence[str]] = lambda artifact: ()  # other files under work/ the artifact relies on
    key_extra: Callable[[Config], str] | None = None  # extra cache-key material, e.g. an input file hash
    summary: Callable[[Any], str] = lambda artifact: ""  # one line of key numbers for the display
