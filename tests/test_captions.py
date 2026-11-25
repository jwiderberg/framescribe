import subprocess

import pytest

from framescribe.captions import parse_captions
from framescribe.ffmpeg import extract_subtitles_srt

SRT = """﻿1
00:00:04,000 --> 00:00:08,000
(Speaker One)
Okay. All right.

2
00:01:02,500 --> 00:01:06,000
Second line
wraps here
"""

VTT = """WEBVTT

NOTE a comment

00:04.000 --> 00:08.000 align:start
<v Alice>Hello <i>there</i></v>

01:00:00.250 --> 01:00:01.000
No speaker
"""


def test_srt_with_bom_speaker_and_multiline():
    caps = parse_captions(SRT)
    assert [(c.start, c.end, c.speaker, c.text) for c in caps] == [
        (4.0, 8.0, "Speaker One", "Okay. All right."),
        (62.5, 66.0, None, "Second line wraps here"),
    ]


def test_vtt_voice_tags_short_timestamps_and_hours():
    caps = parse_captions(VTT)
    assert [(c.start, c.end, c.speaker, c.text) for c in caps] == [
        (4.0, 8.0, "Alice", "Hello there"),
        (3600.25, 3601.0, None, "No speaker"),
    ]


def test_crlf_line_endings():
    assert len(parse_captions(SRT.replace("\n", "\r\n"))) == 2


def test_bad_timing_line_is_an_error():
    with pytest.raises(ValueError, match="unparsable"):
        parse_captions("1\n00:00:04 --> garbage\ntext\n")


def test_embedded_caption_track_is_extracted_whole(tmp_path, letterbox_video):
    srt = tmp_path / "c.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\n(Test Speaker)\nhello world\n\n2\n00:00:02,000 --> 00:00:02,900\nbye\n")
    out = tmp_path / "with_subs.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(letterbox_video), "-i", str(srt),
                    "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text", str(out)], check=True)
    caps = parse_captions(extract_subtitles_srt(out))
    assert [(c.speaker, c.text) for c in caps] == [("Test Speaker", "hello world"), (None, "bye")]
    assert caps[0].start == pytest.approx(0.5, abs=0.01)
