"""Recorded text in Markdown: inert, whatever it says.

Everything framescribe writes into report.md and transcript.md that it did not write itself (on-screen text, step
titles, the vocabulary, pointer labels, spoken and filled-in text, speaker names, the source file name) passes through
`md_text` first. Recorded text can then neither break the document's structure (a newline cannot start a heading or a
table row) nor become a link, an image, emphasis, a table cell, HTML or a code span. That includes the autolinks of
GitHub Flavored Markdown, which need no brackets: a backslash goes before the colon of `http://`, `https://`, `ftp://`
(any `scheme://`), `mailto:` and `xmpp:` and before the dot of `www.`, and the entity of an invisible word joiner
(&#8288;) before the @ of an e-mail address (escapes do not stop an e-mail autolink, which cmark-gfm finds after the
escapes are resolved). Bidirectional and other invisible format characters are removed (fmt.strip_invisible).

The tool's own marks around filled-in words are code spans in Markdown (`[+` ... `+]` and `[?` ... `?]`), put around
the words after they were escaped. Recorded text can never make a code span (its backticks are escaped), so a mark in
recorded text shows as plain bracketed text, rendered or not, and cannot pass for one of the tool's.

timeline.json keeps the text as it is: it is data, not a document.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .fmt import strip_invisible
from .models import TranscriptSegment, TranscriptWord

MARKS = {True: ("[+ ", " +]"), False: ("[? ", " ?]")}  # filled words: confirmed, unconfirmed (timeline.json, text)
MD_MARKS = {True: ("`[+` ", " `+]`"), False: ("`[?` ", " `?]`")}  # the same in Markdown: code spans
MARKS_NOTE = ("The marks are always code spans; recorded text never makes one, so brackets that are not in a code span "
              "are recorded text, even when they look like a mark.")

# C0 controls (newline and tab included), DEL, C1 controls, and the Unicode line and paragraph separators
_CONTROL_RUN = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
# what can start a link, an image, emphasis or strikethrough, a table cell, a code span, or an escape
_ACTIVE = re.compile(r"([\\`*_\[\]()!|~])")
# GFM autolinks: the colon of scheme:// and of mailto: and xmpp:, the dot after www (escaped), the @ of an e-mail
# address (after a character an address can hold; a word joiner goes before it)
_AUTOLINK_COLON = re.compile(r":(?=//)|(?<=mailto):|(?<=xmpp):", re.IGNORECASE)
_WWW_DOT = re.compile(r"(?<=www)\.", re.IGNORECASE)
_EMAIL_AT = re.compile(r"(?<=[A-Za-z0-9.+_-])@")
_AT_PLACEHOLDER = "\x00"  # a control character: none is left in the text when it is put in
WORD_JOINER = "&#8288;"


def one_line(text: str) -> str:
    """Every run of control characters (newlines and tabs included) as one space, the bidirectional and other
    invisible format characters removed (fmt.strip_invisible)."""
    return _CONTROL_RUN.sub(" ", strip_invisible(text))


def md_inline(text: str) -> str:
    """`text` as inert Markdown inside a line: control characters collapsed to a space and invisible format
    characters removed; &, < and > as HTML entities; \\ ` * _ [ ] ( ) ! | ~ backslash-escaped; no autolink (a
    backslash before the colon of scheme://, mailto: and xmpp: and the dot of www., a word joiner entity before the @
    of an e-mail address). Rendered, the text reads as it was: nothing else changes and nothing is cut."""
    t = _EMAIL_AT.sub(_AT_PLACEHOLDER + "@", one_line(text))
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = _ACTIVE.sub(r"\\\1", t)
    t = _WWW_DOT.sub(r"\\.", _AUTOLINK_COLON.sub(r"\\:", t))
    return t.replace(_AT_PLACEHOLDER, WORD_JOINER)


def md_text(text: str) -> str:
    """`md_inline`, and a # at the start escaped too, for text that may begin a line (a heading otherwise)."""
    return re.sub(r"^(\s*)#", r"\1\\#", md_inline(text))


def md_text_start(escaped: str) -> str:
    """Escape a # at the start of text whose parts were escaped with md_inline."""
    return re.sub(r"^(\s*)#", r"\1\\#", escaped)


def marked_text(words: Sequence[TranscriptWord], escape=lambda text: text, marks=MARKS) -> str:
    """The words joined by spaces, each run of filled words in its mark: [+ confirmed +] or [? unconfirmed ?]. With
    `escape` (md_inline) and `marks` MD_MARKS, each word is escaped and the marks are code spans."""
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
        left, right = marks[bool(w.confirmed)]
        out.append(left + " ".join(escape(x.text) for x in words[i:j]) + right)
        i = j
    return " ".join(out)


def md_segment(segment: TranscriptSegment, marks: bool) -> str:
    """A transcript segment's text for Markdown. `marks`: the transcript was cross-checked, so its filled words carry
    the tool's marks (code spans, MD_MARKS), rebuilt from the words when they give exactly the segment's text.
    Otherwise (an imported transcript, or words that do not match) the whole text is escaped, any mark-like text in it
    included."""
    if marks and segment.words and marked_text(segment.words) == segment.text.strip():
        return md_text_start(marked_text(segment.words, md_inline, MD_MARKS))
    return md_text(segment.text.strip())
