"""The Mac app's entry module: `python -m framescribe_app_launcher ARGS` is `python -m framescribe ARGS`, plus a
lifeline.

The app passes the read end of a pipe as the file descriptor named in FRAMESCRIBE_APP_LIFELINE_FD and keeps the
write end open while it runs. When the pipe reaches end-of-file, the app is gone without having stopped this process
(it crashed or was force quit): the process points stdout and stderr at /dev/null and sends itself SIGTERM, which
`framescribe run` handles like Ctrl-C, stopping its own child processes, keeping finished stages cached and writing
run.json. Without the variable it changes nothing.

Installed by app/build_app.sh into the embedded runtime's site-packages; not part of the framescribe package.
"""

from __future__ import annotations

import os
import signal
import threading

LIFELINE_VARIABLE = "FRAMESCRIBE_APP_LIFELINE_FD"


def _watch(fd: int) -> None:
    try:
        while os.read(fd, 4096):
            pass
    except OSError:
        pass
    # nobody reads the events any more: send them nowhere, so that writing them while stopping cannot fail with a
    # broken pipe before the run record (run.json) is written
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.kill(os.getpid(), signal.SIGTERM)


def main() -> None:
    fd = os.environ.pop(LIFELINE_VARIABLE, None)
    if fd is not None and fd.isdigit():
        threading.Thread(target=_watch, args=(int(fd),), name="app-lifeline", daemon=True).start()
    from framescribe.cli import app

    app(prog_name="framescribe")


if __name__ == "__main__":
    main()
