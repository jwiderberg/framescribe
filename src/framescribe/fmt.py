"""Small formatting helpers shared by the transcript renderer and the status display."""

from __future__ import annotations

import math
import re

# C0 controls (newline and tab included), DEL and C1 controls: ESC and CSI start terminal escape sequences
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTROL_BUT_NEWLINE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
# Unicode format characters that reorder or hide text: the bidirectional controls (U+061C, U+200E, U+200F, U+202A
# to U+202E, U+2066 to U+2069: text that reads differently from how it is stored), the deprecated format controls
# U+206A to U+206F, the interlinear annotation marks U+FFF9 to U+FFFB, U+FEFF inside text, and the tag characters
# U+E0000 to U+E007F (invisible text a language model still reads). Zero-width joiners and non-joiners stay: emoji and
# several scripts need them. The tags of an emoji subdivision flag (U+1F3F4, then letters or digits, then U+E007F)
# stay too.
_INVISIBLE = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u206f\ufeff\ufff9-\ufffb]")
_TAGS = re.compile("(\U0001f3f4?)([\U000e0000-\U000e007f]+)")
_FLAG_TAGS = re.compile("[\U000e0030-\U000e0039\U000e0061-\U000e007a]{1,6}\U000e007f")


def timestamp(seconds: float) -> str:
    """HH:MM:SS, truncated. A value that is not a finite number shows as 00:00:00."""
    s = max(0, int(seconds)) if isinstance(seconds, (int, float)) and math.isfinite(seconds) else 0
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def strip_invisible(text: str) -> str:
    """`text` without the format characters that reorder or hide text (see _INVISIBLE); the rest unchanged."""
    text = _INVISIBLE.sub("", text)
    return _TAGS.sub(lambda m: m.group(0) if m.group(1) and _FLAG_TAGS.fullmatch(m.group(2)) else m.group(1), text)


def terminal_text(text: object, keep_newlines: bool = False) -> str:
    """`text` for a terminal: every control character removed (a file name or a message could otherwise move the
    cursor, recolour or retitle the terminal, or hide what was printed), and the bidirectional and other invisible
    format characters of strip_invisible. With `keep_newlines`, newlines stay."""
    return (_CONTROL_BUT_NEWLINE if keep_newlines else _CONTROL).sub("", strip_invisible(str(text)))
