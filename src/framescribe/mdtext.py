"""Recorded text in Markdown: inert, whatever it says.

This module is the boundary between text and the Markdown documents framescribe writes (report.md, transcript.md).
A document is put together from `Md` values only: `document` refuses any other line. An Md value is one of

- `own(text)`: text framescribe wrote itself, a constant of the code, put in as it is;
- `md_text(text)` (or `md_inline`): any other text, escaped;
- `md(template, *values)` and `md_join(sep, values)`: a template of the code with values put in, every value that is
  not Md itself escaped with md_text.

A plain str (an f-string included) is never Md. So text that comes from anywhere but the code (on-screen text, step
titles, the vocabulary, pointer labels, spoken and filled-in text, speaker names, the source file name, and every
field read from an artifact, an imported transcript or a caption file: source names, reason codes, warnings) reaches a
document only escaped: `md` escapes it, and `document` refuses it when it was formatted in without `md`.

Escaped, recorded text can neither break the document's structure (a newline cannot start a heading or a table row,
text at the start of a line or list item cannot start a heading, a list, a thematic break, a block quote, a setext
heading, a code block or a fence) nor become a link, an image, emphasis, a table cell, HTML or a code span. That
includes the autolinks of GitHub Flavored Markdown, which need no brackets: a backslash goes before the colon of
`http://`, `https://`, `ftp://` (any `scheme://`), `mailto:` and `xmpp:` and before the dot of `www.`, in any case, and
the entity of an invisible word joiner (&#8288;) before the @ of an e-mail address (escapes do not stop an e-mail
autolink, which cmark-gfm finds after the escapes are resolved). Bidirectional and other invisible format characters
are removed (fmt.strip_invisible) before any of this, so the tool's own word joiner is the only one.

What this guarantees holds for GitHub Flavored Markdown renderers (cmark-gfm and those that follow it). A viewer that
links bare domain names on its own (example.com/x, without a scheme or www.) goes beyond GFM; the text is not changed
for it.

The tool's own marks around filled-in words are code spans in Markdown (`[+` ... `+]` and `[?` ... `?]`), put around
the words after they were escaped. Recorded text can never make a code span (its backticks are escaped), so a mark in
recorded text shows as plain bracketed text, rendered or not, and cannot pass for one of the tool's.

timeline.json keeps the text as it is: it is data, not a document.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import LiteralString

from .fmt import strip_invisible
from .models import TranscriptSegment, TranscriptWord


class Md(str):
    """Markdown that may go into a document as it is: the tool's own text (`own`), escaped text (`md_text`), or a
    template of the code with escaped values (`md`, `md_join`). Joining or adding plain strings gives a plain str,
    which is not Md: it is escaped again or refused."""

    __slots__ = ()


def own(text: LiteralString) -> Md:
    """Text framescribe wrote itself, a constant of the code: Markdown as it is. Never for text read from anywhere."""
    if not isinstance(text, str):
        raise TypeError(f"own() takes the tool's own text, not {type(text).__name__}")
    return Md(text)


MARKS = {True: ("[+ ", " +]"), False: ("[? ", " ?]")}  # filled words: confirmed, unconfirmed (timeline.json, text)
MD_MARKS = {True: (own("`[+` "), own(" `+]`")), False: (own("`[?` "), own(" `?]`"))}  # the same in Markdown: code spans
MARKS_NOTE = own("The marks are always code spans; recorded text never makes one, so brackets that are not in a code "
                 "span are recorded text, even when they look like a mark.")

# C0 controls (newline and tab included), DEL, C1 controls, and the Unicode line and paragraph separators
_CONTROL_RUN = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
# what can start a link, an image, emphasis or strikethrough, a table cell, a code span, or an escape
_ACTIVE = re.compile(r"([\\`*_\[\]()!|~])")
# GFM autolinks: the colon of scheme:// and of mailto: and xmpp:, the dot after www (escaped), the @ of an e-mail
# address (after a character an address can hold; a word joiner goes before it). In any case: HTTPS://, Mailto:, WWW.
_AUTOLINK_COLON = re.compile(r":(?=//)|(?<=mailto):|(?<=xmpp):", re.IGNORECASE)
_WWW_DOT = re.compile(r"(?<=www)\.", re.IGNORECASE)
_EMAIL_AT = re.compile(r"(?<=[A-Za-z0-9.+_-])@")
_AT_PLACEHOLDER = "\x00"  # a control character: none is left in the text when it is put in
WORD_JOINER = "&#8288;"
# what makes a block at the start of a line or a list item, once md_inline has escaped the rest: # (an ATX heading),
# - or + followed by a space or the end (a list item), a text of nothing but - or = and spaces (a thematic break, a
# setext underline), and a number of up to nine digits followed by a dot and a space or the end (an ordered list).
# * _ ` ~ > < | [ are escaped or entities wherever they are, ) after a number too.
_ORDERED = re.compile(r"\d{1,9}\.(?= |$)")
_RULE = re.compile(r"-[- ]*|=[= ]*")


def one_line(text: str) -> str:
    """Every run of control characters (newlines and tabs included) as one space, the bidirectional and other
    invisible format characters removed (fmt.strip_invisible)."""
    return _CONTROL_RUN.sub(" ", strip_invisible(text))


def md_inline(text: str) -> Md:
    """`text` as inert Markdown inside a line: control characters collapsed to a space and invisible format
    characters removed; &, < and > as HTML entities; \\ ` * _ [ ] ( ) ! | ~ backslash-escaped; no autolink (a
    backslash before the colon of scheme://, mailto: and xmpp: and the dot of www., a word joiner entity before the @
    of an e-mail address). Rendered, the text reads as it was: nothing else changes and nothing is cut."""
    t = _EMAIL_AT.sub(_AT_PLACEHOLDER + "@", one_line(text))
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = _ACTIVE.sub(r"\\\1", t)
    t = _WWW_DOT.sub(r"\\.", _AUTOLINK_COLON.sub(r"\\:", t))
    return Md(t.replace(_AT_PLACEHOLDER, WORD_JOINER))


def block_start(escaped: str) -> Md:
    """Markdown escaped with md_inline, made safe at the start of a line or a list item: leading spaces removed (four
    would start a code block; a renderer does not show them) and a leading block marker escaped (see _ORDERED).
    Rendered, nothing changes."""
    t = escaped.lstrip(" ")
    if (m := _ORDERED.match(t)) is not None:
        return Md(t[: m.end() - 1] + "\\." + t[m.end():])
    if t[:1] == "#" or _RULE.fullmatch(t) or (t[:1] in ("-", "+") and t[1:2] in ("", " ")):
        return Md("\\" + t)
    return Md(t)


def md_text(text: str) -> Md:
    """`text` escaped for any place in a document: md_inline, and safe at the start of a line or list item too."""
    return block_start(md_inline(text))


def md(template: LiteralString, *values: object) -> Md:
    """`template` (the tool's own text, with {} for each value) with the values put in: Md values as they are, every
    other value (a str, a number) escaped with md_text."""
    return Md(template.format(*(v if isinstance(v, Md) else md_text(str(v)) for v in values)))


def md_join(sep: LiteralString, values: Iterable[object]) -> Md:
    """The values joined by `sep` (the tool's own text), each escaped as `md` escapes it."""
    return Md(sep.join(v if isinstance(v, Md) else md_text(str(v)) for v in values))


def document(lines: Iterable[Md]) -> str:
    """The lines of a document joined by newlines. A line that is not Md is refused (TypeError): it was put together
    without `md`, so text in it may not be escaped."""
    out: list[str] = []
    for n, line in enumerate(lines, start=1):
        if not isinstance(line, Md):
            raise TypeError(f"line {n} of the document is not Md (use md(), md_text() or own()): {str(line)[:80]!r}")
        out.append(line)
    return "\n".join(out)


class MarkdownText:
    """fmt.PlainText for Markdown: `fmt` is `md` and `join` is `md_join`, so every value is escaped."""

    fmt = staticmethod(md)
    join = staticmethod(md_join)


MARKDOWN = MarkdownText()


def anchor(heading: str) -> Md:
    """The anchor GitHub-style renderers give a heading: lower case, nothing but letters, digits, _ and -, each space
    a -. Safe in a link target as it is."""
    return Md(re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-"))


def _runs(words: Sequence[TranscriptWord]) -> Iterable[tuple[Sequence[TranscriptWord], bool | None]]:
    """The words in runs: a primary word alone (None), or a run of filled words of the same kind (confirmed: True)."""
    i = 0
    while i < len(words):
        w = words[i]
        if not w.filled:
            yield words[i : i + 1], None
            i += 1
            continue
        j = i
        while j < len(words) and words[j].filled and bool(words[j].confirmed) == bool(w.confirmed):
            j += 1
        yield words[i:j], bool(w.confirmed)
        i = j


def marked_text(words: Sequence[TranscriptWord]) -> str:
    """The words joined by spaces, each run of filled words in its mark: [+ confirmed +] or [? unconfirmed ?]."""
    out = []
    for run, kind in _runs(words):
        text = " ".join(w.text for w in run)
        out.append(text if kind is None else MARKS[kind][0] + text + MARKS[kind][1])
    return " ".join(out)


def md_marked(words: Sequence[TranscriptWord]) -> Md:
    """marked_text for Markdown: each word escaped (md_inline), the marks code spans (MD_MARKS), the start of the
    whole safe at the start of a line (block_start)."""
    out = []
    for run, kind in _runs(words):
        text = md_join(" ", [md_inline(w.text) for w in run])
        out.append(text if kind is None else md("{}{}{}", MD_MARKS[kind][0], text, MD_MARKS[kind][1]))
    return block_start(md_join(" ", out))


def md_segment(segment: TranscriptSegment, marks: bool) -> Md:
    """A transcript segment's text for Markdown. `marks`: the transcript was cross-checked by this tool, so its filled
    words carry the tool's marks (code spans, MD_MARKS), rebuilt from the words when they give exactly the segment's
    text. Otherwise (an imported transcript, or words that do not match) the whole text is escaped, any mark-like text
    in it included."""
    if marks and segment.words and marked_text(segment.words) == segment.text.strip():
        return md_marked(segment.words)
    return md_text(segment.text.strip())
