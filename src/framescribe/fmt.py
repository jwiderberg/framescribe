"""Small formatting helpers shared by the transcript renderer and the status display."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable

# C0 controls (newline and tab included), DEL and C1 controls: ESC and CSI start terminal escape sequences
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTROL_BUT_NEWLINE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")

# Unicode format characters (category Cf) are removed from recorded text, all of them by default: the bidirectional
# controls (text that reads differently from how it is stored), zero-width spaces and joiners such as U+200B and
# U+2060 to U+2064, the soft hyphen, U+180E, U+FEFF, the interlinear annotation marks, the musical beam and phrase
# controls, and the tag characters U+E0000 to U+E007F (invisible text a language model still reads). Kept are only
# the ones legitimate text needs: the zero-width non-joiner and joiner (emoji sequences, Persian, the Indic scripts),
# the Arabic, Syriac and Kaithi signs that span the digits after them (they show), and the format controls of
# Egyptian hieroglyphs and Duployan shorthand. Tag characters stay only in the three RGI emoji subdivision flags
# (U+1F3F4, the tag letters of gbeng, gbsct or gbwls, U+E007F): England, Scotland and Wales.
KEPT_FORMAT = frozenset(
    "‌‍"  # zero-width non-joiner, zero-width joiner
    "؀؁؂؃؄؅۝܏࢐࢑࣢\U000110bd\U000110cd"  # number and end signs
    + "".join(chr(c) for c in range(0x13430, 0x13440))  # Egyptian hieroglyph format controls
    + "\U0001bca0\U0001bca1\U0001bca2\U0001bca3"  # Duployan shorthand format controls
)
RGI_SUBDIVISION_FLAGS = ("gbeng", "gbsct", "gbwls")
_FLAG_SEQUENCE = re.compile("(" + "|".join(
    "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in tag) + "\U000e007f" for tag in RGI_SUBDIVISION_FLAGS) + ")")


def timestamp(seconds: float) -> str:
    """HH:MM:SS, truncated. A value that is not a finite number shows as 00:00:00."""
    s = max(0, int(seconds)) if isinstance(seconds, (int, float)) and math.isfinite(seconds) else 0
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def is_removed_format(c: str) -> bool:
    """A format character (Cf) that strip_invisible removes: every one but KEPT_FORMAT."""
    return unicodedata.category(c) == "Cf" and c not in KEPT_FORMAT


def strip_invisible(text: str) -> str:
    """`text` without the format characters that reorder or hide text (every Cf character but KEPT_FORMAT, tag
    characters but those of an RGI subdivision flag); the rest unchanged."""
    if text.isascii():
        return text
    parts = _FLAG_SEQUENCE.split(text)  # the flags at the odd places, kept as they are
    return "".join(p if i % 2 else "".join(c for c in p if not is_removed_format(c)) for i, p in enumerate(parts))


def terminal_text(text: object, keep_newlines: bool = False) -> str:
    """`text` for a terminal: every control character removed (a file name or a message could otherwise move the
    cursor, recolour or retitle the terminal, or hide what was printed), and the bidirectional and other invisible
    format characters of strip_invisible. With `keep_newlines`, newlines stay."""
    return (_CONTROL_BUT_NEWLINE if keep_newlines else _CONTROL).sub("", strip_invisible(str(text)))


class PlainText:
    """How a text made of fixed words and values is put together for a terminal or a log: the values as they are
    (the terminal display removes control characters itself). mdtext.MARKDOWN does the same for Markdown, escaping
    every value. A text built from `fmt` and `join` (Completeness.summary) is then right for both."""

    @staticmethod
    def fmt(template: str, *values: object) -> str:
        return template.format(*values)

    @staticmethod
    def join(sep: str, values: Iterable[object]) -> str:
        return sep.join(str(v) for v in values)


PLAIN = PlainText()
