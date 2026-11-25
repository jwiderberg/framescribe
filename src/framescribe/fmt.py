"""Small formatting helpers shared by the transcript renderer and the status display."""

from __future__ import annotations


def timestamp(seconds: float) -> str:
    """HH:MM:SS, truncated."""
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
