"""Live status of the visual and ocr stages in the three display modes."""

import io
import json
import sys

import pytest
from rich.console import Console
from synth import encode, hold, page

from framescribe.config import Config
from framescribe.pipeline import run_pipeline
from framescribe.status import JsonRenderer, PlainRenderer, RichRenderer

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the ocr stage needs Apple Vision")


def test_visual_and_ocr_report_real_progress_in_all_three_display_modes(tmp_path):
    p1 = page(1, header="Lighthouse")
    p2 = page(2, bg=(20, 30, 70), fg=(235, 235, 245), header="Orchard")
    video = encode(hold(p1, 3) + hold(p2, 3), tmp_path / "talk.mp4")
    srt = tmp_path / "t.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello there.\n")
    events: list[dict] = []
    run_pipeline(video, tmp_path / "out", Config(transcript=srt, layout_samples=4, content_rect=(0, 0, 640, 360)),
                 sink=events.append)

    vis = [e for e in events if e["event"] == "stage_progress" and e["stage"] == "visual"]
    assert {e["unit"] for e in vis} == {"frames", "key frames"}
    decode = [e for e in vis if e["unit"] == "frames"]
    assert decode[0]["done"] == 0 and decode[-1]["done"] == decode[-1]["total"] and len(decode) >= 2
    assert any("change detection" in e["note"] for e in decode)
    keys = [e for e in vis if e["unit"] == "key frames"]
    assert keys[-1]["done"] == keys[-1]["total"] == 2
    ocr_ev = [e for e in events if e["event"] == "stage_progress" and e["stage"] == "ocr"]
    assert ocr_ev[-1]["done"] == ocr_ev[-1]["total"] == 2 and ocr_ev[-1]["unit"] == "frames"
    assert [e["t"] for e in events] == sorted(e["t"] for e in events)

    out = io.StringIO()
    json_render = JsonRenderer(out)
    for e in events:
        json_render(e)
    parsed = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(parsed) == len(events)
    assert any(p.get("stage") == "ocr" and p["event"] == "stage_progress" for p in parsed)

    out = io.StringIO()
    plain = PlainRenderer(out)
    for e in events:
        plain(e)
    text = out.getvalue()
    assert "visual" in text and "ocr" in text and "states" in text and "lines/state" in text

    console = Console(file=io.StringIO(), force_terminal=False, width=140, record=True)
    rich = RichRenderer(console)
    midway = next(e for e in events if e["event"] == "stage_progress" and e["stage"] == "ocr" and e["done"] >= 1)
    for e in events:
        rich(e)
        if e is midway:
            console.print(rich.panel())
            break
    panel = console.export_text()
    assert "visual" in panel and "ocr" in panel and "running" in panel and "lines" in panel and "frames/s" in panel and "state 1/2" in panel
