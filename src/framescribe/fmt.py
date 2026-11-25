"""Small formatting helpers shared by the transcript renderer and the status display."""

from __future__ import annotations

import math
import re

# C0 controls (newline and tab included), DEL and C1 controls: ESC and CSI start terminal escape sequences
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTROL_BUT_NEWLINE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def timestamp(seconds: float) -> str:
    """HH:MM:SS, truncated. A value that is not a finite number shows as 00:00:00."""
    s = max(0, int(seconds)) if isinstance(seconds, (int, float)) and math.isfinite(seconds) else 0
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def terminal_text(text: object, keep_newlines: bool = False) -> str:
    """`text` for a terminal: every control character removed (a file name or a message could otherwise move the
    cursor, recolour or retitle the terminal, or hide what was printed). With `keep_newlines`, newlines stay."""
    return (_CONTROL_BUT_NEWLINE if keep_newlines else _CONTROL).sub("", str(text))
