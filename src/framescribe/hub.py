"""The speech model in the local Hugging Face cache.

`cached_model_files` reads only the local cache. `download_model` is the one function in the project that opens a
network connection; `framescribe setup` is its only caller.

The speech model is pinned: MODEL_REVISION is a commit of the model repository on Hugging Face, and MODEL_SHA256 the
SHA-256 of each of its two files at that commit. `setup` downloads exactly that commit and checks both files after the
download, `doctor` and `setup` check them in the cache (`model_problem`, a local read, no network), and `run`, the
speech stage and the scripts use that commit's files only. Changing MODEL_REVISION changes the model, so it needs a
new VERSION of the asr.parakeet stage as well. `go_offline` switches Hugging Face's client to offline
mode for the whole process; `run`, the `asr.parakeet` stage (through `doctor.uncached_model_problem`, before it loads
the model, so a program that calls the pipeline as a library is covered too) and the scripts call it. `huggingface_hub`
is imported inside the functions so that it can be switched to offline mode before it is first imported.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

# The two files parakeet-mlx's `from_pretrained` reads from a model repository.
MODEL_FILES = ("config.json", "model.safetensors")
MODEL_REPO = "mlx-community/parakeet-tdt-0.6b-v3"
# The commit of MODEL_REPO this version of framescribe uses, and the SHA-256 of its files there. The values come from
# the snapshot of that commit in a local Hugging Face cache: the revision from its refs/main, the model's hash
# computed from the file and equal to the name Hugging Face gave the file's blob (its LFS object id).
MODEL_REVISION = "ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15"
MODEL_SHA256 = {
    "config.json": "f320f1292511f34ec47f513755fe20fd01dbfc09a925d42730e66059a6e1ef4c",
    "model.safetensors": "05e01c7f396c298cf7d23f61da7b504adeab698f0aaeafd9c82d198625464592",
}
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


def revision(repo: str) -> str | None:
    """The pinned commit for the default model; another repository (a changed Config) is taken at its main branch."""
    return MODEL_REVISION if repo == MODEL_REPO else None


def cached_model_files(repo: str) -> dict[str, Path] | None:
    """The model's files in the local cache, at the pinned revision, or None when any of them is missing. Never uses
    the network."""
    from huggingface_hub import try_to_load_from_cache

    found: dict[str, Path] = {}
    for name in MODEL_FILES:
        path = try_to_load_from_cache(repo, name, revision=revision(repo))
        if not isinstance(path, str) or not Path(path).is_file():
            return None
        found[name] = Path(path)
    return found


def model_dir(repo: str) -> Path | None:
    """The folder of the pinned snapshot, which parakeet-mlx's `from_pretrained` loads as a local path (it would take
    the branch main from a repository name)."""
    files = cached_model_files(repo)
    return files["model.safetensors"].parent if files else None


def file_sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def model_problem(repo: str, files: dict[str, Path], sha256: Callable[[Path], str] = file_sha256) -> str | None:
    """For the pinned model: the first file whose SHA-256 is not the pinned one, as a message; None when every file
    matches (or the repository is not the pinned one, which has no recorded hashes). Reads the files, no network."""
    if repo != MODEL_REPO:
        return None
    for name, expected in MODEL_SHA256.items():
        got = sha256(files[name])
        if got != expected:
            return f"{name} of {repo}@{MODEL_REVISION[:12]} has SHA-256 {got[:16]}..., expected {expected[:16]}..."
    return None


def discard_mismatched(repo: str, sha256: Callable[[Path], str] = file_sha256) -> list[Path]:
    """Remove from the cache every file of the pinned snapshot whose SHA-256 is wrong (the snapshot's link and the blob
    it points to), so that the next download fetches it again. The removed paths."""
    files = cached_model_files(repo)
    removed: list[Path] = []
    if files is None or repo != MODEL_REPO:
        return removed
    for name, path in files.items():
        if sha256(path) != MODEL_SHA256[name]:
            blob = Path(os.path.realpath(path))
            for p in (path, blob):
                if os.path.lexists(p):
                    os.unlink(p)
                    removed.append(p)
    return removed


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

    rev = revision(repo)
    if on_progress is None:
        paths = [Path(hf_hub_download(repo, name, revision=rev)) for name in MODEL_FILES]
    else:
        paths = [Path(hf_hub_download(repo, name, revision=rev, tqdm_class=progress_reporter(name, on_progress)))
                 for name in MODEL_FILES]
    problem = model_problem(repo, dict(zip(MODEL_FILES, paths)))
    if problem is not None:
        discard_mismatched(repo)
        raise ModelIntegrityError(f"the downloaded model does not match the pinned one, so it was removed again: "
                                  f"{problem}")
    return paths


class ModelIntegrityError(RuntimeError):
    """A downloaded model file has another SHA-256 than the pinned one."""


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
