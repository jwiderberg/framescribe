"""Child process handling: when the parent is interrupted (Ctrl-C), terminated (SIGTERM) or hung up on (SIGHUP),
its child processes are stopped before it exits.

A parent killed with SIGKILL cannot do this: a child in its own session (whisperkit-cli) then keeps running.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

TERMINATE_GRACE_SECONDS = 5


TERMINATING_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


class Terminated(KeyboardInterrupt):
    """Raised by the SIGTERM and SIGHUP handler, so that termination unwinds like Ctrl-C."""

    def __init__(self, signum: int) -> None:
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def install_termination_handlers() -> None:
    """SIGTERM and SIGHUP (a closed terminal) raise Terminated. Their default action ends the process at once,
    leaving a child in its own session running and the run record unwritten."""

    def handler(signum, frame):
        raise Terminated(signum)

    for sig in TERMINATING_SIGNALS:
        signal.signal(sig, handler)


def _stop(proc: subprocess.Popen, sig: int, own_group: bool) -> None:
    try:
        if own_group:
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)
    except ProcessLookupError:
        pass


@contextmanager
def child_process(cmd: list[str], **popen_kwargs: Any) -> Iterator[subprocess.Popen]:
    proc = subprocess.Popen(cmd, **popen_kwargs)
    own_group = bool(popen_kwargs.get("start_new_session"))
    try:
        yield proc
    finally:
        if proc.poll() is None:
            _stop(proc, signal.SIGTERM, own_group)
            try:
                proc.wait(TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                _stop(proc, signal.SIGKILL, own_group)
                proc.wait()
