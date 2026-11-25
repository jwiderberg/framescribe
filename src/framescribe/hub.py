"""The speech model in the local Hugging Face cache.

`cached_model_files` reads only the local cache. `download_model` is the one function in the project that opens a
network connection; `framescribe setup` is its only caller. `go_offline` switches Hugging Face's client to offline
mode for the whole process; `run`, the `asr.parakeet` stage (through `doctor.uncached_model_problem`, before it loads
the model, so a program that calls the pipeline as a library is covered too) and the scripts call it. `huggingface_hub`
is imported inside the functions so that it can be switched to offline mode before it is first imported.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

# The two files parakeet-mlx's `from_pretrained` reads from a model repository.
MODEL_FILES = ("config.json", "model.safetensors")
MODEL_SIZE_GB = 2.5  # the download, measured: 2.51 GB
MIN_FREE_GB = 6.0  # the model, its temporary download, and room to work


def go_offline() -> None:
    """Make Hugging Face's client refuse network access for the rest of this process."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    import sys

    constants = sys.modules.get("huggingface_hub.constants")
    if constants is not None:  # imported before this call: its copy of the variable has been read already
        constants.HF_HUB_OFFLINE = True


def offline_requested() -> bool:
    return os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}


def cache_dir() -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE)


def cached_model_files(repo: str) -> dict[str, Path] | None:
    """The model's files in the local cache, or None when any of them is missing. Never uses the network."""
    from huggingface_hub import try_to_load_from_cache

    found: dict[str, Path] = {}
    for name in MODEL_FILES:
        path = try_to_load_from_cache(repo, name)
        if not isinstance(path, str) or not Path(path).is_file():
            return None
        found[name] = Path(path)
    return found


def free_gb(path: Path) -> float:
    """Free disk space, in GB (10**9 bytes), on the volume that holds `path` (or its nearest existing parent)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1e9


ProgressCallback = Callable[[str, int, int], None]  # (file name, bytes done, bytes in total; 0 when unknown)
PROGRESS_INTERVAL = 0.5  # seconds between two progress calls for one file


def download_model(repo: str, on_progress: ProgressCallback | None = None) -> list[Path]:
    """Download the model's files into the Hugging Face cache. Needs the network.

    Without `on_progress`, Hugging Face's own progress bars go to stderr. With it, there are no bars; `on_progress` is
    called with the bytes done per file instead, at most every PROGRESS_INTERVAL seconds and once when a file is
    complete."""
    from huggingface_hub import hf_hub_download

    if on_progress is None:
        return [Path(hf_hub_download(repo, name)) for name in MODEL_FILES]
    return [Path(hf_hub_download(repo, name, tqdm_class=progress_reporter(name, on_progress))) for name in MODEL_FILES]


class _Discard:
    def write(self, text: str) -> None:
        pass

    def flush(self) -> None:
        pass


def progress_reporter(name: str, on_progress: ProgressCallback, interval: float = PROGRESS_INTERVAL,
                      now: Callable[[], float] = time.monotonic) -> type:
    """A tqdm class for Hugging Face's `tqdm_class` that draws nothing and reports to `on_progress`. A plain tqdm
    subclass, so that Hugging Face passes it only tqdm's own arguments. Only the first bar made from the class reports:
    a Xet download makes two for one file (bytes written, then bytes received), and the first is the one that reaches
    the file's size."""
    from tqdm.std import tqdm

    class Reporter(tqdm):
        reporting: "Reporter | None" = None

        def __init__(self, *args, **kwargs) -> None:
            kwargs["file"] = _Discard()
            kwargs["disable"] = False  # a disabled bar does not count
            super().__init__(*args, **kwargs)
            if Reporter.reporting is None:
                Reporter.reporting = self
            self._reported_at: float | None = None
            self._report(force=True)

        def update(self, n: float | None = 1) -> bool | None:
            shown = super().update(n)
            self._report(force=bool(self.total) and self.n >= self.total)
            return shown

        def _report(self, force: bool) -> None:
            if Reporter.reporting is not self:
                return
            t = now()
            if force or self._reported_at is None or t - self._reported_at >= interval:
                self._reported_at = t
                on_progress(name, int(self.n), int(self.total or 0))

    return Reporter
