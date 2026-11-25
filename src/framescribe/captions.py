"""Parse SRT and WebVTT into captions. A leading "(Name)" line or a <v Name> tag gives the speaker."""

from __future__ import annotations

import re

from .models import Caption

_TIME = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_CUE_LINE = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
_SPEAKER_LINE = re.compile(r"^\(([^()]+)\)$")
_VOICE_TAG = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+)>")
_ANY_TAG = re.compile(r"</?[^>]+>")


def _seconds(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_captions(text: str) -> list[Caption]:
    """Parse SRT or WebVTT text. Raises ValueError when a block has times but no parsable cue."""
    captions: list[Caption] = []
    blocks = re.split(r"\n\s*\n", text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = block.strip("\n").split("\n")
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue  # WEBVTT header, NOTE and STYLE blocks
        m = _CUE_LINE.match(lines[idx])
        if not m:
            raise ValueError(f"unparsable cue timing line: {lines[idx]!r}")
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
