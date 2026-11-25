"""Parse SRT and WebVTT into captions. A leading "(Name)" line or a <v Name> tag gives the speaker."""

from __future__ import annotations

import re

from .models import Caption

_TIME = r"(?:(\d{1,6}):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"  # at most 6 digits of hours: no huge numbers
_CUE_LINE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
_SPEAKER_LINE = re.compile(r"^\(([^()]+)\)$")
# Linear: the name stops at the next "<" or ">", so a line of many "<v " does not make every match attempt scan to
# the end of the line (the old [^>]+ took time quadratic in the line length).
_VOICE_TAG = re.compile(r"<v(?:\.[^\s<>]*)?[ \t]+([^<>]+)>")
_ANY_TAG = re.compile(r"</?[^<>]+>")
MAX_LINE_CHARS = 2000  # a caption line longer than this is cut: no cue is that long, and every regex stays cheap
MAX_TIMING_CHARS = 200  # a longer timing line is not one (WebVTT cue settings included)


def _seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_captions(text: str) -> list[Caption]:
    """Parse SRT or WebVTT text. Raises ValueError when a block has times but no parsable cue (pydantic's
    ValidationError, a ValueError, for times out of range)."""
    captions: list[Caption] = []
    blocks = re.split(r"\n\s*\n", text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = block.strip("\n").split("\n")
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue  # WEBVTT header, NOTE and STYLE blocks
        lines = [ln if i == idx else ln[:MAX_LINE_CHARS] for i, ln in enumerate(lines)]
        m = _CUE_LINE.match(lines[idx]) if len(lines[idx]) <= MAX_TIMING_CHARS else None
        if not m:
            raise ValueError(f"unparsable cue timing line: {lines[idx][:80]!r}")
        g = m.groups()
        start, end = _seconds(*g[0:4]), _seconds(*g[4:8])
        speaker, body = _split_speaker(lines[idx + 1 :])
        if body:
            captions.append(Caption(start=start, end=end, speaker=speaker, text=body))
    return captions


def _split_speaker(lines: list[str]) -> tuple[str | None, str]:
    speaker: str | None = None
    if lines and (m := _SPEAKER_LINE.match(lines[0].strip())):
        speaker, lines = m.group(1).strip(), lines[1:]
    joined = " ".join(ln.strip() for ln in lines if ln.strip())
    if speaker is None and (m := _VOICE_TAG.search(joined)):
        speaker = m.group(1).strip()
    body = _ANY_TAG.sub("", joined)
    return speaker, re.sub(r"\s+", " ", body).strip()
