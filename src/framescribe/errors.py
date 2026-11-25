"""Errors that are shown to the user as a message, not a traceback."""

from __future__ import annotations


class StageError(RuntimeError):
    """A run failed for a reason the user can act on. The message is shown as is.

    `kind`: a machine-readable name of the reason for hosts (`error_kind` in the failure events), None when there is
    no particular one."""

    kind: str | None = None


class WordOrderError(StageError):
    """A backend produced word timings that are out of order or out of range."""


class NoAudioError(StageError):
    """The video has no audio stream, so there is nothing to transcribe."""

    kind = "no_audio"


class GateError(StageError):
    """The completeness gate failed. `figures` are the completeness figures (without the gap list), for run.json."""

    kind = "incomplete_transcript"

    def __init__(self, message: str, figures: dict) -> None:
        super().__init__(message)
        self.figures = figures
