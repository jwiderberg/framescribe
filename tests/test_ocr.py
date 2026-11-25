"""OCR: Apple Vision on rendered text (macOS only), the line diff, and the stage's caching and de-duplication."""

import shutil
import sys
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw
from synth import WORDS, font, hold, page, tall_document

from framescribe import ocr_engine
from framescribe.config import Config
from framescribe.models import OcrLine, Rect, VisualArtifact
from framescribe.ocrdiff import diff_lines, text_similarity
from framescribe.pipeline import run_pipeline
from framescribe.stages import ocr as ocr_stage
from framescribe.stages.base import Context, StageError

vision = pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision runs on macOS only")


def line(text, x, y, w=120, h=12, conf=1.0):
    return OcrLine(text=text, rect=Rect(x=x, y=y, w=w, h=h), confidence=conf)


def iou(a: Rect, b: Rect) -> float:
    ix = max(0, min(a.x1, b.x1) - max(a.x, b.x))
    iy = max(0, min(a.y1, b.y1) - max(a.y, b.y))
    inter = ix * iy
    return inter / (a.w * a.h + b.w * b.h - inter)


# ---- diff (no Vision needed) -------------------------------------------------------------------------------


class TestDiff:
    base = [line("Weekly train timetable", 40, 50), line("Cancelled 12 trains", 40, 80), line("Platforms", 40, 110),
            line("Refreshing times from the depot", 40, 140), line("Depot", 40, 170)]

    def test_identical_text_with_ocr_jitter_has_no_diff(self):
        jitter = [line("Weekly train timetable", 41, 51), line("Cancelled 12 trains", 39, 80),
                  line("Platfoms", 40, 111), line("Refreshing times from the depo", 40, 141), line("Depot", 40, 170)]
        added, removed, scroll, shift = diff_lines(self.base, jitter, 960, 516)
        assert added == [] and removed == [] and scroll is None and shift == 0

    def test_changed_added_and_removed_lines(self):
        cur = [self.base[0], line("Cancelled 13 trains", 40, 80), self.base[2], line("Extra service added", 40, 200)]
        added, removed, _, _ = diff_lines(self.base, cur, 960, 516)
        assert sorted(a.text for a in added) == ["Cancelled 13 trains", "Extra service added"]  # a changed number is a change
        assert sorted(r.text for r in removed) == ["Cancelled 12 trains", "Depot", "Refreshing times from the depot"]

    def test_scroll_moves_every_line_but_only_new_and_gone_lines_are_reported(self):
        prev = [line(f"Row number {w} of the table", 40, 40 + 30 * i) for i, w in enumerate(WORDS[:14])]
        cur = [line(f"Row number {w} of the table", 40, 40 + 30 * (i - 4)) for i, w in enumerate(WORDS[:14]) if i >= 4]
        cur += [line(f"Row number {w} of the table", 40, 40 + 30 * (i - 4)) for i, w in enumerate(WORDS[14:18], start=14)]
        added, removed, scroll, shift = diff_lines(prev, cur, 960, 516, visual_dy=-120)
        assert shift == -120
        assert len(added) == 4 and len(removed) == 4  # rows that scrolled in and out, not all 14
        assert scroll is not None and scroll.visual_dy == -120 and scroll.ocr_dy == -120
        assert scroll.matched_lines == 10 and scroll.consistent_lines == 10

    def test_shift_is_found_from_the_text_when_the_visual_stage_gave_none(self):
        prev = [line(f"Stop {w} on the route", 40, 40 + 30 * i) for i, w in enumerate(WORDS[:10])]
        cur = [line(f"Stop {w} on the route", 40, 40 + 30 * i + 60) for i, w in enumerate(WORDS[:10])]
        added, removed, _, shift = diff_lines(prev, cur, 960, 516)
        assert shift == 60 and added == [] and removed == []

    def test_a_visual_scroll_that_the_text_disagrees_with_is_reported(self):
        prev = [line(f"Stop {w} on the route", 40, 40 + 30 * i) for i, w in enumerate(WORDS[:10])]
        cur = [line(f"Stop {w} on the route", 40, 40 + 30 * i + 60) for i, w in enumerate(WORDS[:10])]
        _, _, scroll, _ = diff_lines(prev, cur, 960, 516, visual_dy=-200)
        assert scroll is not None and scroll.ocr_dy == 60 and scroll.consistent_lines == 0

    def test_short_texts_must_match_exactly(self):
        assert text_similarity("run", "rum") == 0.0 and text_similarity("run", "run") == 1.0
        assert text_similarity("24% done", "25% done") == 0.0
        assert text_similarity("returned 17 apr 2031", "retumed 17 apr 2031") > 0.9


# ---- language mapping (no Vision needed) -------------------------------------------------------------------

OFFERED = ["en-US", "fr-FR", "de-DE", "es-ES", "pt-BR", "zh-Hans", "zh-Hant", "yue-Hans", "ko-KR", "ar-SA", "ar-AE"]


class TestVisionLanguages:
    @pytest.mark.parametrize("language, expected", [
        ("en", "en-US"), ("de", "de-DE"), ("fr", "fr-FR"), ("pt", "pt-BR"), ("ko", "ko-KR"),
        ("ar", "ar-SA"),  # the first of several variants
        ("zh", "zh-Hans"), ("DE", "de-DE"),
    ])
    def test_a_bare_code_becomes_a_supported_tag(self, language, expected):
        assert ocr_engine.vision_languages(language, OFFERED) == [expected]

    @pytest.mark.parametrize("language", ["en-US", "zh-Hant", "ar-AE", "yue-Hans"])
    def test_a_supported_tag_is_passed_on_unchanged(self, language):
        assert ocr_engine.vision_languages(language, OFFERED) == [language]

    def test_zh_prefers_hans_even_when_hant_is_listed_first(self):
        assert ocr_engine.vision_languages("zh", ["zh-Hant", "zh-Hans"]) == ["zh-Hans"]

    def test_the_first_listed_variant_is_used_when_there_is_no_preference(self):
        assert ocr_engine.vision_languages("de", ["de-AT", "de-DE"]) == ["de-AT"]

    def test_a_language_prefix_is_not_a_primary_subtag(self):
        with pytest.raises(StageError, match="does not support language"):
            ocr_engine.vision_languages("y", OFFERED)

    @pytest.mark.parametrize("language", ["sv", "xx", "de-AT", "en-GB"])
    def test_a_language_without_a_supported_variant_fails_and_lists_the_offer(self, language):
        with pytest.raises(StageError, match=f"does not support language \\['{language}'\\].*'de-DE'"):
            ocr_engine.vision_languages(language, OFFERED)

    @vision
    def test_english_and_german_resolve_against_the_real_vision_list(self):
        offered = ocr_engine.supported_languages()
        assert ocr_engine.vision_languages("en", offered) == ["en-US"]
        assert ocr_engine.vision_languages("de", offered)[0].startswith("de-")
        assert ocr_engine.vision_languages("zh", offered) == ["zh-Hans"]


# ---- Apple Vision on rendered text -------------------------------------------------------------------------


def render_lines(specs, size=(640, 360)):
    """specs: [(text, font size, x, y)] -> image and ground-truth rects (the text's own pixel box)."""
    img = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    truth = []
    for text, fs, x, y in specs:
        d.text((x, y), text, fill=(20, 20, 20), font=font(fs))
        x0, y0, x1, y1 = d.textbbox((x, y), text, font=font(fs))
        truth.append((text, Rect(x=x0, y=y0, w=x1 - x0, h=y1 - y0)))
    return img, truth


@vision
def test_text_of_several_sizes_is_read_with_correct_boxes():
    assert font(12).getbbox("H")[3] - font(12).getbbox("H")[1] == 9  # the small size has a 9 px cap height
    specs = [("Autumn route summary", 36, 20, 10), ("Choose a station to continue", 24, 20, 70),
             ("Renewed 17 April 2031 by the front desk", 16, 20, 120),
             ("Platforms checked against the timetable", 12, 20, 160),
             ("Refreshing times from the depot", 12, 20, 190), ("Status Early Delayed Cancelled", 12, 320, 190)]
    img, truth = render_lines(specs)
    lines = ocr_engine.recognise(img, 2, ["en-US"])
    for text, rect in truth:
        best = max(lines, key=lambda ln: iou(ln.rect, rect))
        assert iou(best.rect, rect) >= 0.5, (text, best, rect)
        norm = lambda s: "".join(ch for ch in s.lower() if ch.isalnum())
        assert norm(text) in norm(best.text) or norm(best.text) in norm(text), (text, best.text)
    for ln in lines:  # rects are in the original image's pixels, not the 2x upscaled ones
        assert ln.rect.x1 <= 640 and ln.rect.y1 <= 360


@vision
def test_added_and_removed_lines_between_two_frames():
    one, _ = render_lines([("Weekly train timetable", 16, 30, 30), ("Cancelled trains twelve", 16, 30, 70),
                           ("Refreshing times from the depot", 16, 30, 110), ("Depot control panel", 16, 30, 150)])
    two, _ = render_lines([("Weekly train timetable", 16, 30, 30), ("Printing finished just now", 16, 30, 70),
                           ("Refreshing times from the depot", 16, 30, 110), ("Depot control panel", 16, 30, 150),
                           ("Collect the printed tickets", 16, 30, 190)])
    a, b = ocr_engine.recognise(one, 2, ["en-US"]), ocr_engine.recognise(two, 2, ["en-US"])
    added, removed, _, _ = diff_lines(a, b, 640, 360)
    assert sorted(x.text.lower() for x in added) == ["collect the printed tickets", "printing finished just now"]
    assert [x.text.lower() for x in removed] == ["cancelled trains twelve"]


@vision
def test_a_scroll_does_not_flood_added_and_removed():
    doc = Image.fromarray(tall_document(3, h=900))
    top, scrolled = doc.crop((0, 100, 640, 460)), doc.crop((0, 100 + 104, 640, 460 + 104))
    a, b = ocr_engine.recognise(top, 2, ["en-US"]), ocr_engine.recognise(scrolled, 2, ["en-US"])
    assert len(a) >= 8
    for dy in (None, -104):
        added, removed, scroll, shift = diff_lines(a, b, 640, 360, dy)
        assert abs(shift + 104) <= 3
        assert len(added) + len(removed) <= 0.4 * (len(a) + len(b)), (len(added), len(removed), len(a), len(b))
    naive_added = [x for x in b if not any(x.text == y.text and abs(x.rect.y - y.rect.y) < 6 for y in a)]
    assert len(naive_added) > 2 * len(added)  # without the shift nearly everything would look new
    assert scroll is not None and scroll.consistent_lines >= 0.6 * scroll.matched_lines


# ---- stage: dedupe, missing frames, cache -------------------------------------------------------------------


def test_identical_keyframes_are_recognised_once(tmp_path, monkeypatch):
    from framescribe.models import Change, VisualState

    frames = tmp_path / "frames"
    frames.mkdir()
    Image.fromarray(page(1)).save(frames / "s0001_t00-00-00.jpg", quality=90)
    shutil.copy(frames / "s0001_t00-00-00.jpg", frames / "s0002_t00-00-05.jpg")
    Image.fromarray(page(2)).save(frames / "s0003_t00-00-09.jpg", quality=90)
    ch = Change(kind="page", kind_confidence=0.9, changed_fraction=0.5, changed_rect=None, scroll_dy=None)
    states = [VisualState(id=i, start=i, end=i + 1, keyframe_time=i, keyframe_path=f"frames/{n}", settled=True, change=ch, cursor=[])
              for i, n in enumerate(sorted(p.name for p in frames.iterdir()), start=1)]
    from framescribe.models import CapInfo

    art = VisualArtifact(fps_sampled=2, duration=3, content_rect=Rect(x=0, y=0, w=640, h=360), states=states,
                         cap=CapInfo(max_states_per_minute=20, min_state_seconds=1.5, max_event_seconds=10, hit=False,
                                     forced_closes=0, suppressed_changes=0), cursor_samples=0, wall_seconds=0)
    calls = []

    def fake(image, scale, languages):
        calls.append(image.size)
        return [OcrLine(text=f"read {len(calls)}", rect=Rect(x=1, y=1, w=5, h=5), confidence=0.9)]

    monkeypatch.setattr(ocr_engine, "recognise", fake)
    monkeypatch.setattr(ocr_engine, "require_macos", lambda: None)
    ctx = Context(config=Config(), source=tmp_path / "v.mp4", run_dir=tmp_path)
    ctx.artifacts["visual"] = art
    events = []
    ctx.emit = lambda event, **f: events.append((event, f))
    result = ocr_stage.run(ctx)
    assert len(calls) == 2 and result.frames_recognised == 2
    assert [s.duplicate_of for s in result.states] == [None, 1, None]
    assert result.states[1].lines == result.states[0].lines
    progress = [f for e, f in events if e == "stage_progress"]
    assert progress[-1]["done"] == 2 and progress[-1]["total"] == 2 and progress[-1]["unit"] == "frames"
    assert result.states[0].added == result.states[0].lines and result.states[0].diff_against is None
    assert result.states[2].diff_against == 2

    (frames / "s0002_t00-00-05.jpg").unlink()
    with pytest.raises(StageError, match="key frame of state 2 is missing"):
        ocr_stage.run(ctx)


def test_ocr_refuses_to_run_off_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(StageError, match="macOS only"):
        ocr_engine.require_macos()


@vision
def test_pipeline_writes_visual_and_ocr_artifacts_and_a_missing_keyframe_is_a_miss(tmp_path):
    from synth import encode

    p1 = page(1, header="Lighthouse")
    p2 = page(2, bg=(20, 30, 70), fg=(235, 235, 245), header="Orchard")
    video = encode(hold(p1, 3) + hold(p2, 3) + hold(p1, 3), tmp_path / "talk.mp4")
    srt = tmp_path / "t.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello there.\n")
    cfg = Config(transcript=srt, layout_samples=4, content_rect=(0, 0, 640, 360))
    rec, run_dir = run_pipeline(video, tmp_path / "out", cfg)
    st = {s.stage: s.status for s in rec.stages}
    assert st["visual"] == "miss" and st["ocr"] == "miss"
    from framescribe.models import OcrArtifact

    vis = VisualArtifact.model_validate_json((run_dir / "work/visual.json").read_text())
    ocr = OcrArtifact.model_validate_json((run_dir / "work/ocr.json").read_text())
    assert [s.state_id for s in ocr.states] == [s.id for s in vis.states] and len(vis.states) == 3
    assert any("Orchard" in ln.text for ln in ocr.states[1].lines)
    assert ocr.engine == "apple-vision" and ocr.scale == 2 and ocr.language_correction
    assert all(ln.rect.x1 <= 640 and ln.rect.y1 <= 360 for s in ocr.states for ln in s.lines)

    rec, _ = run_pipeline(video, tmp_path / "out", cfg)
    assert {s.stage: s.status for s in rec.stages}["visual"] == "hit"

    (run_dir / vis.states[1].keyframe_path).unlink()
    rec, _ = run_pipeline(video, tmp_path / "out", cfg)
    st = {s.stage: s.status for s in rec.stages}
    assert st == {"ingest": "hit", "asr": "hit", "visual": "miss", "ocr": "hit", "report": "hit"}
    assert (run_dir / vis.states[1].keyframe_path).is_file()

    rec, _ = run_pipeline(video, tmp_path / "out", cfg, force=["visual"])
    st = {s.stage: s.status for s in rec.stages}
    assert st["visual"] == "forced" and st["ocr"] == "forced" and st["ingest"] == "hit"
    rec, _ = run_pipeline(video, tmp_path / "out", cfg, force=["ocr"])
    assert {s.stage: s.status for s in rec.stages}["visual"] == "hit"
    assert {s.stage: s.status for s in rec.stages}["ocr"] == "forced"
    rec, _ = run_pipeline(video, tmp_path / "out", replace(cfg, visual_keep_fraction=0.02))
    st = {s.stage: s.status for s in rec.stages}
    assert st["ingest"] == "hit" and st["visual"] == "miss" and st["ocr"] == "miss"  # a visual setting changes both
