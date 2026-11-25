"""Recorded text in Markdown: inert, whatever it says.

Everything framescribe writes into report.md and transcript.md that it did not write itself (on-screen text, step
titles, the vocabulary, pointer labels, spoken and filled-in text, speaker names, the source file name) passes through
`md_text` first. Recorded text can then neither break the document's structure (a newline cannot start a heading or a
table row) nor become a link, an image, emphasis, a table cell, HTML or a code span. The tool's own marks around
filled-in words ([+ ... +] and [? ... ?]) are added after the words inside them were escaped, so a mark in recorded text
shows as escaped text and cannot pass for one of the tool's.

timeline.json keeps the text as it is: it is data, not a document.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .models import TranscriptSegment, TranscriptWord

MARKS = {True: ("[+ ", " +]"), False: ("[? ", " ?]")}  # filled words: confirmed, unconfirmed

# C0 controls (newline and tab included), DEL, C1 controls, and the Unicode line and paragraph separators
_CONTROL_RUN = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
# what can start a link, an image, emphasis or strikethrough, a table cell, a code span, or an escape
_ACTIVE = re.compile(r"([\\`*_\[\]()!|~])")


def one_line(text: str) -> str:
    """Every run of control characters (newlines and tabs included) as one space."""
    return _CONTROL_RUN.sub(" ", text)


def md_inline(text: str) -> str:
    """`text` as inert Markdown inside a line: control characters collapsed to a space; &, < and > as HTML entities;
    \\ ` * _ [ ] ( ) ! | ~ backslash-escaped. Nothing else changes and nothing is cut."""
    t = one_line(text)
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _ACTIVE.sub(r"\\\1", t)


def md_text(text: str) -> str:
    """`md_inline`, and a # at the start escaped too, for text that may begin a line (a heading otherwise)."""
    return re.sub(r"^(\s*)#", r"\1\\#", md_inline(text))


def md_text_start(escaped: str) -> str:
    """Escape a # at the start of text whose parts were escaped with md_inline."""
    return re.sub(r"^(\s*)#", r"\1\\#", escaped)


def marked_text(words: Sequence[TranscriptWord], escape=lambda text: text) -> str:
    """The words joined by spaces, each run of filled words in its mark: [+ confirmed +] or [? unconfirmed ?]. With
    `escape` (md_text), each word is escaped before the marks are put around it."""
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if not w.filled:
            out.append(escape(w.text))
            i += 1
            continue
        j = i
        while j < len(words) and words[j].filled and bool(words[j].confirmed) == bool(w.confirmed):
            j += 1
        left, right = MARKS[bool(w.confirmed)]
        out.append(left + " ".join(escape(x.text) for x in words[i:j]) + right)
        i = j
    return " ".join(out)


def md_segment(segment: TranscriptSegment, marks: bool) -> str:
    """A transcript segment's text for Markdown. `marks`: the transcript was cross-checked, so its filled words carry
    the tool's marks, rebuilt from the words when they give exactly the segment's text. Otherwise (an imported
    transcript, or words that do not match) the whole text is escaped, any mark-like text in it included."""
    if marks and segment.words and marked_text(segment.words) == segment.text.strip():
        return md_text_start(marked_text(segment.words, md_inline))
    return md_text(segment.text.strip())
