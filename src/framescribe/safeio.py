"""Writing into the run folder without following a symbolic link planted there.

Every file the pipeline writes goes through here: a new file is made with a name nobody can predict (`mkstemp` in the
same folder, which never follows a link) and renamed over the target (`os.replace` replaces a link at the target
itself, never the file it points to). Logs are opened with O_NOFOLLOW. A run starts only when its run folder, `work/`
and `frames/` are real folders owned by the user who runs it (`check_run_folders`).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from .errors import StageError

_UMASK = os.umask(0)
os.umask(_UMASK)
FILE_MODE = 0o666 & ~_UMASK  # what open() would give a new file


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` through a new temporary file in the same folder, renamed over it."""
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    with atomic_output(path) as f:
        f.write(data)


@contextlib.contextmanager
def atomic_output(path: Path, suffix: str = ".tmp") -> Iterator[IO[bytes]]:
    """A binary file object for writing `path`: a new file next to it, renamed over `path` when the block ends without
    an error, removed otherwise."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=suffix)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "wb") as f:
            yield f
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def temporary_sibling(path: Path, suffix: str) -> Path:
    """A new, empty regular file next to `path` with a name nobody can predict, for a tool (ffmpeg) that writes its
    output by name. The caller renames it over `path` or removes it."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=suffix)
    os.fchmod(fd, FILE_MODE)
    os.close(fd)
    return Path(tmp)


def open_log(path: Path, *, truncate: bool = False) -> IO[str]:
    """A log file for appending (or, with `truncate`, writing from the start), opened with O_NOFOLLOW: a symbolic
    link at `path` raises a StageError instead of being written through."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_TRUNC if truncate else os.O_APPEND)
    try:
        fd = os.open(path, flags, 0o666)
    except OSError as e:
        if os.path.islink(path):
            raise StageError(f"refusing to write the log {path}: it is a symbolic link") from None
        raise StageError(f"cannot write the log {path}: {e.strerror}") from None
    return os.fdopen(fd, "w" if truncate else "a", encoding="utf-8", errors="replace")


def open_binary_log(path: Path) -> IO[bytes]:
    """`open_log` for raw bytes, written from the start."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, 0o666)
    except OSError as e:
        if os.path.islink(path):
            raise StageError(f"refusing to write the log {path}: it is a symbolic link") from None
        raise StageError(f"cannot write the log {path}: {e.strerror}") from None
    return os.fdopen(fd, "wb")


def check_real_folder(path: Path, what: str) -> None:
    """When `path` exists it must be a folder (not a symbolic link to one) owned by the user running framescribe."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise StageError(f"refusing to use the {what} {path}: it is a symbolic link")
    if not stat.S_ISDIR(st.st_mode):
        raise StageError(f"refusing to use the {what} {path}: it is not a folder")
    if st.st_uid != os.getuid():
        raise StageError(f"refusing to use the {what} {path}: it belongs to another user (uid {st.st_uid})")


def check_run_folders(run_dir: Path) -> None:
    """The run folder, `work/` and `frames/`: real folders of this user, or not there yet."""
    check_real_folder(run_dir, "run folder")
    check_real_folder(run_dir / "work", "work folder")
    check_real_folder(run_dir / "frames", "frames folder")


def remove_tree(path: Path, run_dir: Path) -> None:
    """Remove `path` (a folder the pipeline made inside the run folder): a symbolic link there is removed itself, a
    real folder only when it resolves to a place inside the resolved run folder; anything else raises."""
    if os.path.islink(path):
        os.unlink(path)
        return
    if not path.exists():
        return
    real, root = os.path.realpath(path), os.path.realpath(run_dir)
    if not path.is_dir() or os.path.commonpath([real, root]) != root or real == root:
        raise StageError(f"refusing to remove {path}: not a folder inside the run folder {run_dir}")
    shutil.rmtree(real)


def remove_if_symlink(path: Path) -> None:
    """Before a tool writes `path` by name: a symbolic link there is removed, so that it cannot be written through."""
    if os.path.islink(path):
        os.unlink(path)
