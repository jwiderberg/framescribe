"""Stage cache: content-addressed keys and on-disk artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from .models import Artifact
from .safeio import atomic_write_text

EDGE_BYTES = 4 * 1024 * 1024
VOLATILE_FIELDS = frozenset({"wall_seconds", "pass_seconds"})  # measurements that differ between runs of identical work

A = TypeVar("A", bound=Artifact)

# The side files a stage may record next to its artifact, relative to work/: the extracted audio, the report stage's
# three files in the run folder, and key frames. A cached record naming anything else is not trusted (a cache miss).
_SIDE_FILE = re.compile(r"audio\.wav|\.\./(report\.md|timeline\.json|ANALYSIS_PROMPT\.md)"
                        r"|\.\./frames/s\d{4,}_t\d{2,}-\d{2}-\d{2}\.jpg")


def allowed_side_file(name: object) -> bool:
    return isinstance(name, str) and _SIDE_FILE.fullmatch(name) is not None


def sha256_edges(path: Path) -> str:
    """sha256 of the first and last 4 MB of a file (the whole file when it is smaller)."""
    size = path.stat().st_size
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(EDGE_BYTES))
        if size > EDGE_BYTES:
            f.seek(max(EDGE_BYTES, size - EDGE_BYTES))
            h.update(f.read(EDGE_BYTES))
    return h.hexdigest()


def source_fingerprint(path: Path) -> str:
    st = path.stat()
    material = f"{st.st_size}:{st.st_mtime_ns}:{sha256_edges(path)}"
    return hashlib.sha256(material.encode()).hexdigest()


def stage_key(
    *,
    fingerprint: str,
    stage: str,
    version: int,
    config: Mapping[str, object],
    dep_keys: Mapping[str, str],
    dep_digests: Mapping[str, str],
    extra: str = "",
) -> str:
    """Cache key for one stage: source, stage identity and version, used config, upstream keys, and the content
    digests of the upstream artifacts. An upstream stage recomputed under the same key with a different result
    (e.g. after an interrupted --force) changes the digest, so this stage's old result is not reused."""
    material = json.dumps(
        {
            "source": fingerprint,
            "stage": stage,
            "version": version,
            "config": config,
            "deps": dict(sorted(dep_keys.items())),
            "dep_digests": dict(sorted(dep_digests.items())),
            "extra": extra,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


class ArtifactStore:
    """`work/<file>` holds the artifact JSON; `work/<file>.key` holds the key it was computed with and the
    sizes of the stage's other output files, so a truncated or replaced side file reads as a miss."""

    def __init__(self, work_dir: Path, log: Callable[[str], None] = lambda message: None) -> None:
        self.work_dir = work_dir
        self.log = log  # why a cached artifact was not trusted

    def _key_path(self, filename: str) -> Path:
        return self.work_dir / (filename + ".key")

    def load(self, filename: str, key: str, model: type[A]) -> A | None:
        """The cached artifact if its stored key equals `key`, every recorded side file still has its
        recorded size, and it still validates; else None."""
        try:
            record = json.loads(self._key_path(filename).read_text())
            if record["key"] != key:
                return None
            for name, size in record["files"].items():
                if not allowed_side_file(name):
                    self.log(f"cached {filename} names an unexpected file {name!r}; computing it again")
                    return None
                side = self.work_dir / name
                if os.path.islink(side) or side.stat().st_size != size:
                    return None
            artifact_path = self.work_dir / filename
            if os.path.islink(artifact_path):
                self.log(f"cached {filename} is a symbolic link; computing it again")
                return None
            return model.model_validate_json(artifact_path.read_text())
        except ValidationError as e:
            self.log(f"cached {filename} does not validate ({e.error_count()} error(s), first: "
                     f"{e.errors()[0]['msg'] if e.errors() else '?'}); computing it again")
            return None
        except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError, KeyError, TypeError, AttributeError,
                UnicodeDecodeError):
            return None

    def digest(self, filename: str) -> str:
        """sha256 of the artifact and of the side files saved with it, as they are on disk now. The artifact is
        hashed without its VOLATILE_FIELDS, so recomputing identical work gives the same digest."""
        record = json.loads(self._key_path(filename).read_text())
        h = hashlib.sha256()
        h.update(filename.encode() + b"\0" + self._artifact_digest(filename))
        for name in sorted(record["files"]):
            with (self.work_dir / name).open("rb") as f:
                h.update(name.encode() + b"\0" + hashlib.file_digest(f, "sha256").digest())
        return h.hexdigest()

    def _artifact_digest(self, filename: str) -> bytes:
        raw = (self.work_dir / filename).read_bytes()
        data = json.loads(raw)
        stable = {k: v for k, v in data.items() if k not in VOLATILE_FIELDS}
        if len(stable) == len(data):
            return hashlib.sha256(raw).digest()  # nothing to leave out: the digest stays what it was
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).digest()

    def invalidate(self, filename: str) -> None:
        """Call before recomputing a stage: until `save` succeeds, nothing is a hit."""
        self._key_path(filename).unlink(missing_ok=True)

    def save(self, filename: str, key: str, artifact: Artifact, side_files: Sequence[str] = ()) -> None:
        path = self.work_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        self.invalidate(filename)
        _atomic_write(path, artifact.model_dump_json(indent=1))
        sizes = {name: (self.work_dir / name).stat().st_size for name in side_files}
        _atomic_write(self._key_path(filename), json.dumps({"key": key, "files": sizes}) + "\n")


def write_if_changed(path: Path, text: str) -> bool:
    """Write `text` to `path` atomically unless the file already holds exactly that; True when it was written.
    A run that changes nothing leaves its output files (and their modification times) alone."""
    try:
        if not os.path.islink(path) and path.read_text() == text:
            return False
    except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError):
        pass
    _atomic_write(path, text)
    return True


def _atomic_write(path: Path, text: str) -> None:
    """A new temporary file next to `path`, renamed over it: a link at `path` (or at any predictable temporary name)
    is replaced, never written through (safeio.atomic_write_text)."""
    atomic_write_text(path, text)
