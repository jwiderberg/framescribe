"""The report stage on small synthetic artifacts: steps, timeline events, the Markdown, and the stage's caching."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

import pytest
from report_synth import (
    line,
    make_frames,
    ocr,
    ostate,
    random_run,
    regular_states,
    segment,
    transcript,
    visual,
    vstate,
)

from framescribe import __version__
from framescribe.config import Config
from framescribe.models import CursorSample, OcrScroll, Rect, TimelineArtifact
from framescribe.pipeline import run_pipeline
from framescribe.report_md import PROMPT, group_rows, render_report, slug, step_heading, vocabulary
from framescribe.stages.report import STAGE as REPORT_STAGE
from framescribe.steps import (
    build_steps,
    choose_extras,
    choose_main,
    choose_title,
    find_chrome,
    page_boundaries,
    step_blocks,
)
from framescribe.timeline import (
    Sample,
    build_timeline,
    is_collapsed,
    nearest_line,
    pointer_event,
    said_paragraphs,
    scroll_amount,
    screen_events,
)


def blocks_of(states, segs=(), min_s=30, max_s=240):
    return step_blocks(visual(states), transcript(list(segs)), min_s, max_s)


# ---- step boundaries ---------------------------------------------------------------------------------------


class TestBoundaries:
    def test_steps_start_at_confident_page_changes_and_at_the_first_state(self):
        states = regular_states(12, 10.0, {4: "page", 8: "page"})
        states[6] = vstate(7, 60, 70, kind="page", conf=0.55)  # not confident enough to start a step
        assert page_boundaries(states) == [0, 4, 8]
        assert blocks_of(states) == [(0, 4), (4, 8), (8, 12)]

    def test_short_block_merges_into_the_previous_one(self):
        # blocks (0,4) 40 s, (4,6) 20 s: too short, joins the one before it, (6,10) 40 s, (10,14) 40 s
        states = regular_states(14, 10.0, {4: "page", 6: "page", 10: "page"})
        assert blocks_of(states) == [(0, 6), (6, 10), (10, 14)]

    def test_short_first_block_merges_into_the_next(self):
        states = regular_states(10, 10.0, {2: "page"})
        assert blocks_of(states) == [(0, 10)]

    def test_merging_cascades(self):
        states = regular_states(9, 10.0, {3: "page", 4: "page", 5: "page", 6: "page"})
        assert blocks_of(states) == [(0, 6), (6, 9)]  # the three 10 s blocks join the one before them

    def test_long_block_splits_at_a_pause_that_coincides_with_a_screen_change(self):
        states = regular_states(60, 10.0)  # one 600 s block
        states[30] = vstate(31, 300, 310, kind="minor")  # a minor change at a pause, right at the middle, is not a split point
        segs = [segment(0, 0, 296), segment(1, 299, 396), segment(2, 399, 600)]  # pauses 296..299 and 396..399
        blocks = blocks_of(states, segs, max_s=400)
        assert blocks == [(0, 40), (40, 60)]  # the state starting at 400 s, within 2 s of the second pause

    def test_long_block_without_pause_splits_at_the_sentence_boundary_nearest_the_middle(self):
        states = regular_states(60, 10.0)
        segs = [segment(i, i * 20.0, i * 20.0 + 19.9) for i in range(30)]  # no pauses; boundaries every 20 s
        blocks = blocks_of(states, segs, max_s=400)
        assert blocks == [(0, 30), (30, 60)]

    def test_every_block_complies_after_repeated_splitting(self):
        states = regular_states(200, 10.0)  # 2000 s
        blocks = blocks_of(states, [segment(i, i * 5.0, i * 5.0 + 4.0) for i in range(400)])
        assert all(states[b - 1].end - states[a].start <= 240 for a, b in blocks)
        assert all(states[b - 1].end - states[a].start >= 30 for a, b in blocks)
        assert blocks[0][0] == 0 and blocks[-1][1] == 200
        assert all(x[1] == y[0] for x, y in zip(blocks, blocks[1:]))

    def test_a_single_long_state_is_left_alone(self):
        assert blocks_of([vstate(1, 0, 500, kind="initial")]) == [(0, 1)]

    @pytest.mark.parametrize("seed", range(40))
    def test_random_runs_partition_the_states_and_respect_the_limits(self, seed):
        v, t, _ = random_run(seed)
        blocks = step_blocks(v, t, 30, 240)
        covered = [i for a, b in blocks for i in range(a, b)]
        assert covered == list(range(len(v.states)))
        for a, b in blocks:
            length = v.states[b - 1].end - v.states[a].start
            assert length <= 240 or b - a == 1


# ---- chrome, titles, frames ---------------------------------------------------------------------------------


class TestContent:
    def test_chrome_is_text_in_more_than_sixty_percent_of_states(self):
        states = [ostate(i, [line("  Home  ", 5, 5), line("Settings", 5, 30)] + ([line("Report", 5, 60)] if i <= 6 else []))
                  for i in range(1, 11)]
        states[0].lines.append(line("home", 5, 90))  # case and spacing do not matter; counted once per state
        assert find_chrome(ocr(states)) == {"home", "settings"}  # "report" is in exactly 60 %: not more than 60 %

    def test_title_is_the_tallest_non_chrome_line_in_the_upper_part(self):
        s = ostate(1, [line("Navigation", 5, 10, h=30), line("Big heading", 50, 40, h=26), line("Smaller", 50, 20, h=18),
                       line("Huge but low", 50, 400, h=60)])
        assert choose_title(s, {"navigation"}, 516) == "Big heading"

    def test_title_ties_go_to_the_topmost_line_and_text_is_not_corrected(self):
        s = ostate(1, [line("Lower titlle", 50, 80, h=20), line("Upper titel", 50, 50, h=20)])
        assert choose_title(s, set(), 516) == "Upper titel"

    def test_title_falls_back_to_the_step_number(self):
        states = regular_states(4, 20.0)
        os_ = ocr([ostate(i, [line("Menu", 5, 5)]) for i in range(1, 5)])
        plans = build_steps(visual(states), transcript([]), os_, {"menu"}, 30, 240, 3)
        assert [p.title for p in plans] == ["Step 1"] and plans[0].title_is_fallback

    def test_main_frame_is_the_state_with_most_speech_preferring_settled(self):
        states = [vstate(1, 0, 10), vstate(2, 10, 20), vstate(3, 20, 40, settled=False), vstate(4, 40, 50)]
        segs = [segment(0, 12, 14), segment(1, 22, 39), segment(2, 41, 45)]
        assert choose_main(states, segs).id == 4  # state 3 is unsettled: of the others, state 4 has 4 s of speech, state 2 has 2 s
        states[2] = vstate(3, 20, 40)  # now settled too: the 17 s of speech wins
        assert choose_main(states, segs).id == 3

    def test_main_frame_tie_goes_to_the_longest_state(self):
        states = [vstate(1, 0, 10), vstate(2, 10, 30)]
        assert choose_main(states, []).id == 2

    def test_extra_frames_are_ranked_spaced_and_never_minor(self):
        states = [
            vstate(1, 0, 20, kind="initial"),
            vstate(2, 20, 30, kind="partial", frac=0.5),  # score 5
            vstate(3, 24, 34, kind="scroll", frac=0.9),  # score 9, only 4 s after state 2
            vstate(4, 40, 60, kind="page", frac=0.4),  # score 8
            vstate(5, 70, 72, kind="minor", frac=1.0),  # minor: never
            vstate(6, 80, 90, kind="partial", frac=0.3),  # score 3
            vstate(7, 100, 110, kind="partial", frac=0.2),  # score 2
        ]
        main = states[0]
        got = choose_extras(states, main, 3)
        assert [s.id for s in got] == [3, 4, 6]  # 3 beats 2 (4 s apart); 5 is minor; 7 would be the fourth
        assert choose_extras(states, main, 0) == []


# ---- timeline events ---------------------------------------------------------------------------------------


class TestEvents:
    def test_said_paragraphs_break_at_screen_events_speaker_changes_and_after_thirty_seconds(self):
        segs = [segment(i, i * 5.0, i * 5.0 + 4.0) for i in range(6)]  # starts 0, 5, ... 25
        assert [[s.id for s in p] for p in said_paragraphs(segs, [])] == [[0, 1, 2, 3, 4, 5]]
        assert [[s.id for s in p] for p in said_paragraphs(segs, [12.0])] == [[0, 1, 2], [3, 4, 5]]
        assert [[s.id for s in p] for p in said_paragraphs(segs, [10.0])] == [[0, 1], [2, 3, 4, 5]]  # at the second's start: between
        long = [segment(i, i * 5.0, i * 5.0 + 4.9) for i in range(9)]
        assert [len(p) for p in said_paragraphs(long, [])] == [6, 3]  # 30 s limit measured from the paragraph start

    def test_speaker_change_breaks_a_paragraph(self):
        segs = [segment(0, 0, 2, speaker="A"), segment(1, 2, 4, speaker="A"), segment(2, 4, 6, speaker="B")]
        assert [[s.id for s in p] for p in said_paragraphs(segs, [])] == [[0, 1], [2]]

    def test_minor_changes_do_not_break_a_paragraph_and_runs_collapse_into_one_event(self):
        states = [vstate(1, 0, 5, kind="initial"), vstate(2, 5, 8, kind="minor"), vstate(3, 8, 12, kind="minor"),
                  vstate(4, 12, 20, kind="partial"), vstate(5, 20, 22, kind="minor"), vstate(6, 22, 30, kind="scroll", dy=-100)]
        os_ = {s.id: ostate(s.id) for s in states}
        events = screen_events(states, os_, set())
        assert [(e.kind, e.state_ids) for e in events] == [("initial", [1]), ("minor", [2, 3]), ("partial", [4]), ("minor", [5]),
                                                          ("scroll", [6])]
        assert (events[1].time, events[1].end) == (5, 12)
        breaks = [e.time for e in events if e.kind != "minor"]
        assert breaks == [0, 12, 22]

    def test_a_minor_change_that_brings_two_lines_of_text_is_its_own_event_and_breaks_a_paragraph(self):
        # a tide-table panel sliding in changes few pixels but carries text the speaker talks about
        panel = vstate(2, 5, 8, kind="minor")
        panel.change.changed_rect = Rect(x=600, y=50, w=300, h=200)
        states = [vstate(1, 0, 5, kind="initial"), panel, vstate(3, 8, 12, kind="minor")]
        os_ = {1: ostate(1), 2: ostate(2, [line("Low tide at six", 620, 60), line("High tide at noon", 620, 90),
                                         line("Elsewhere", 10, 400)]),
               3: ostate(3, [line("Only one line", 620, 60)])}
        os_[3].added = os_[3].lines
        events = screen_events(states, os_, set())
        assert [(e.kind, e.state_ids, e.end) for e in events] == [("initial", [1], None), ("minor", [2], None),
                                                                 ("minor", [3], 12)]
        assert events[1].appeared == ["Low tide at six", "High tide at noon"]  # only lines where the pixels changed
        segs = [segment(1, 3.0, 4.5), segment(2, 6.0, 7.0)]
        assert [[s.id for s in p] for p in said_paragraphs(segs, [e.time for e in events if not is_collapsed(e)])] == [[1], [2]]

    def test_report_links_every_key_frame_at_least_once(self, tmp_path):
        for seed in range(6):
            v, t, o = random_run(seed)
            md = render_report(build(v, t, o), v, o, t, __version__)
            linked = {unquote(x) for x in re.findall(r"\]\((frames/[^)]+)\)", md)}
            assert {s.keyframe_path for s in v.states} <= linked, seed

    @pytest.mark.parametrize(
        "dy, ocr_dy, expected",
        [(-120, -118, -120),  # large, OCR agrees within 8 px: amount stated (negative = scrolled down)
         (130, 125, 130),  # up
         (-120, -100, None),  # OCR disagrees by more than 8 px
         (-120, None, None),  # OCR found no match
         (-20, -20, None)],  # 25 px or less: no amount
    )
    def test_scroll_amount_rule(self, dy, ocr_dy, expected):
        s = vstate(2, 5, 9, kind="scroll", dy=dy)
        sc = OcrScroll(visual_dy=dy, ocr_dy=ocr_dy, matched_lines=10, consistent_lines=10)
        assert scroll_amount(s, ostate(2, scroll=sc)) == expected

    def test_scroll_without_an_ocr_check_gets_no_amount(self):
        assert scroll_amount(vstate(2, 5, 9, kind="scroll", dy=-120), ostate(2, scroll=None)) is None

    def test_appeared_and_gone_leave_out_chrome_and_are_capped(self):
        added = [line(f"Item {i}", 10, 10 + 20 * i) for i in range(12)] + [line("Home", 10, 300), line("###", 10, 330)]
        removed = [line(f"Old {i}", 10, 10 + 20 * i) for i in range(7)]
        events = screen_events([vstate(1, 0, 5, kind="page")], {1: ostate(1, added, added, removed)}, {"home"})
        assert events[0].appeared == [f"Item {i}" for i in range(8)]
        assert events[0].gone == [f"Old {i}" for i in range(4)]


class TestPointer:
    cue = "click on this button"
    sample = Sample(t=50.0, x=100.0, y=105.0, state_id=1)
    state = ostate(1, [line("Save", 90, 100, w=60, h=12), line("Far away", 600, 400)])

    def para(self, text=cue, start=48.0, end=52.0):
        from framescribe.timeline import said_event
        return said_event([segment(0, start, end, text)])

    def test_cue_sample_in_window_and_line_within_reach_gives_a_hint(self):
        e = pointer_event(self.para(), [self.sample], {1: self.state})
        assert e is not None and e.type == "pointer" and e.text == "Save" and e.time == 50.0

    def test_line_within_forty_pixels_counts_and_further_does_not(self):
        near = Sample(t=50.0, x=100.0, y=100 + 12 + 39, state_id=1)
        far = Sample(t=50.0, x=100.0, y=100 + 12 + 41, state_id=1)
        assert nearest_line(near, self.state.lines).text == "Save"
        assert nearest_line(far, self.state.lines) is None

    def test_no_cue_word_no_hint(self):
        assert pointer_event(self.para("fine so it runs"), [self.sample], {1: self.state}) is None

    def test_cue_words_match_whole_words_only(self):
        assert pointer_event(self.para("a thistle and a linkage"), [self.sample], {1: self.state}) is None
        assert pointer_event(self.para("Hover over it"), [self.sample], {1: self.state}) is not None

    def test_sample_outside_three_seconds_gives_no_hint(self):
        late = Sample(t=55.5, x=100.0, y=105.0, state_id=1)  # 3.5 s after the paragraph ends at 52
        assert pointer_event(self.para(), [late], {1: self.state}) is None
        edge = Sample(t=55.0, x=100.0, y=105.0, state_id=1)
        assert pointer_event(self.para(), [edge], {1: self.state}) is not None

    def test_no_on_screen_line_near_the_point_gives_no_hint(self):
        away = Sample(t=50.0, x=400.0, y=250.0, state_id=1)
        assert pointer_event(self.para(), [away], {1: self.state}) is None

    def test_a_sample_is_used_once(self):
        used: set[float] = set()
        assert pointer_event(self.para(), [self.sample], {1: self.state}, used) is not None
        assert pointer_event(self.para(), [self.sample], {1: self.state}, used) is None


# ---- whole timeline and markdown ----------------------------------------------------------------------------


def build(v, t, o, min_s=30, max_s=240, extra=3):
    chrome = find_chrome(o)
    plans = build_steps(v, t, o, chrome, min_s, max_s, extra)
    return build_timeline("talk.mp4", "parakeet", v, t, o, plans, chrome, min_s, max_s)


@pytest.mark.parametrize("seed", range(25))
def test_every_segment_once_and_every_state_in_exactly_one_step(seed):
    v, t, o = random_run(seed)
    tl = build(v, t, o)
    segs = [i for s in tl.steps for i in s.segment_ids]
    assert sorted(segs) == [s.id for s in t.segments]
    said = [i for s in tl.steps for e in s.events if e.type == "said" for i in e.segment_ids]
    assert sorted(said) == [s.id for s in t.segments]  # and in SAID lines, verbatim
    texts = {s.id: s.text for s in t.segments}
    for st in tl.steps:
        for e in st.events:
            if e.type == "said":
                assert e.text == " ".join(texts[i] for i in e.segment_ids)
    states = [i for s in tl.steps for i in s.state_ids]
    assert states == [s.id for s in v.states]
    for st in tl.steps:
        assert st.main_state_id in st.state_ids and set(st.extra_state_ids) <= set(st.state_ids)
        times = [e.time for e in st.events if e.type != "pointer"]
        assert times == sorted(times)


def run_dir_for(tmp_path: Path, seed: int = 3):
    v, t, o = random_run(seed)
    make_frames(tmp_path, v)
    tl = build(v, t, o)
    return v, t, o, tl, render_report(tl, v, o, t, __version__)


class TestMarkdown:
    def test_every_link_and_image_resolves_and_every_index_link_has_a_heading(self, tmp_path):
        v, t, o, tl, md = run_dir_for(tmp_path)
        targets = re.findall(r"\]\(([^)#][^)]*)\)", md)
        assert targets
        for target in targets:
            assert (tmp_path / unquote(target)).is_file(), target
        anchors = {slug(step_heading(s)) for s in tl.steps}
        index_links = re.findall(r"\| \[(\d+)\]\(#([^)]+)\)", md)
        assert len(index_links) == len(tl.steps)
        assert all(a in anchors for _, a in index_links)
        assert len(anchors) == len(tl.steps)

    def test_layout_and_no_html(self, tmp_path):
        v, t, o, tl, md = run_dir_for(tmp_path)
        h2 = re.findall(r"^## (.+)$", md, re.M)
        assert h2[:3] == ["How to read this document", "On-screen vocabulary", "Steps"]
        assert h2[-1] == "Coverage and caveats" and sum(x.startswith("Step ") for x in h2) == len(tl.steps)
        assert md.count("![Step ") == len(tl.steps)
        assert re.search(r"!\[Step 1 main frame, state s\d{4} at \d\d:\d\d:\d\d\]\(frames/s\d{4}_t00-00-00\.jpg\)", md)
        assert not re.search(r"</?[a-z]+[ >]", md)
        assert f"framescribe {__version__}" in md

    def test_header_has_the_completeness_row(self):
        from framescribe.models import Completeness

        v = visual([vstate(1, 0, 40, kind="initial"), vstate(2, 40, 50)])
        t = transcript([segment(0, 1, 2)])
        o = ocr([ostate(1), ostate(2)])
        t.completeness = Completeness(primary="parakeet", primary_words=812, sources=["parakeet-pass2", "captions"],
                                      filled_words=9, filled_by_source={"captions": 9}, filled_share=0.011,
                                      confirmed=1, unconfirmed=2, rejected=3, rejected_by_reason={"fragment": 3})
        md = render_report(build(v, t, o), v, o, t, __version__)
        header = md.split("## How to read this document")[0]
        assert ("| Completeness | 812 words from parakeet; cross-checked against the second parakeet pass and the "
                "embedded captions; filled 9 words (1.1%; 9 from captions): 1 confirmed, 2 unconfirmed, 3 rejected gaps "
                "(3 fragment) |") in header

    @staticmethod
    def _rendered(sources, **kw):
        from framescribe.models import Completeness

        v = visual([vstate(1, 0, 40, kind="initial"), vstate(2, 40, 50)])
        t = transcript([segment(0, 1, 2)])
        o = ocr([ostate(1), ostate(2)])
        t.completeness = Completeness(primary="parakeet", primary_words=100, sources=sources, filled_words=0,
                                      filled_by_source={}, filled_share=0.0, confirmed=0, unconfirmed=0, rejected=0,
                                      **kw.pop("figures", {}))
        return render_report(build(v, t, o), v, o, t, __version__, **kw)

    @staticmethod
    def _said_note(md):
        return next(x for x in md.split("\n") if x.startswith("- SAID lines"))

    def test_how_to_read_names_only_the_sources_of_this_run(self):
        both = self._said_note(self._rendered(["parakeet-pass2", "captions"]))
        assert "(a second decode of the audio and the video's captions)" in both
        assert "`[+` ... `+]` when at least two of them" in both and "always code spans" in both and "WhisperKit" not in both and "another speech recognition" not in both
        pass_only = self._said_note(self._rendered(["parakeet-pass2"]))
        assert "filled in from a second decode of the audio, the only other source" in pass_only
        assert "`[+` ... `+]` does not occur" in pass_only and "caption" not in pass_only and "another speech recognition" not in pass_only
        every = self._said_note(self._rendered(["parakeet-pass2", "whisperkit", "captions"]))
        assert "(a second decode of the audio, another speech recognition (WhisperKit) and the video's captions)" in every
        none = self._said_note(self._rendered([]))
        assert "No other transcript" in none and "another speech recognition" not in none

    def test_how_to_read_of_an_imported_transcript_has_no_sources(self):
        v = visual([vstate(1, 0, 40, kind="initial"), vstate(2, 40, 50)])
        t = transcript([segment(0, 1, 2)])
        o = ocr([ostate(1), ostate(2)])
        note = self._said_note(render_report(build(v, t, o), v, o, t, __version__))
        assert "imported" in note and "another speech recognition" not in note

    def test_header_says_plainly_that_the_completeness_limit_was_overridden(self):
        figures = dict(figures={"above_limit": True, "limit": 0.03, "warning": "9 of the transcript's 109 words (8.3%) were filled in"})
        md = self._rendered(["parakeet-pass2"], max_filled_share=0.03, **figures)
        row = next(x for x in md.split("\n") if x.startswith("| Completeness |"))
        # the warning is read from the artifact: escaped like any other text from there
        assert row.startswith("| Completeness | **ABOVE THE USUAL LEVEL: 9 of the transcript's 109 words \\(8.3%\\) were filled in.**")
        assert "ABOVE THE USUAL 3.0%: check the filled-in words" in row  # the summary says it too
        plain = next(x for x in self._rendered(["parakeet-pass2"], max_filled_share=0.03).split("\n")
                     if x.startswith("| Completeness |"))
        assert "ABOVE" not in plain

    def test_header_counts_the_speech_warnings_without_their_spans(self):
        warnings = ["chunk 10.00-20.00 s: 40 words without punctuation kept (the decode with the most words of 3)",
                    "pass 2: chunk 12.00-22.00 s: 38 words without punctuation kept (the decode with the most words of 2)",
                    "chunk 30.00-40.00 s: no words, although 6.0 s of it is above the pause threshold, also after a retry",
                    "no pause near 50.00 s: cut at 50.20 s between two words (0.10 s apart, -40.0 dBFS)"]
        md = self._rendered(["parakeet-pass2"], speech_warnings=warnings)
        header = md.split("## How to read this document")[0]
        row = next(x for x in header.split("\n") if x.startswith("| Speech recognition warnings |"))
        assert "2 chunks kept without punctuation, 1 chunk that stayed empty" in row
        assert "10.00" not in header and "30.00" not in header
        assert "Speech recognition warnings" not in self._rendered(["parakeet-pass2"], speech_warnings=warnings[3:])
        assert "Speech recognition warnings" not in self._rendered(["parakeet-pass2"])

    def test_the_speech_warning_phrases_are_the_ones_the_speech_stage_writes(self):
        import inspect

        from framescribe.report_md import BARE_WARNING, EMPTY_WARNING
        from framescribe.stages import asr_parakeet

        source = " ".join(inspect.getsource(asr_parakeet.decode_pass).split())
        assert BARE_WARNING in source and EMPTY_WARNING in source

    def test_html_in_on_screen_text_is_escaped(self):
        v = visual([vstate(1, 0, 40, kind="initial"), vstate(2, 40, 50)])
        o = ocr([ostate(1, [line("<script>alert(1)</script>", 10, 10, h=30), line("a | b", 10, 200)]), ostate(2)])
        md = render_report(build(v, transcript([segment(0, 1, 2)]), o), v, o, transcript([segment(0, 1, 2)]), __version__)
        # entities now, not a backslash: no < is left anywhere but in the How-to-read note's code span `<`
        assert "&lt;script&gt;alert\\(1\\)&lt;/script&gt;" in md and md.replace("`<`", "").count("<") == 0
        assert "a \\| b" in md

    def test_group_rows_joins_lines_on_the_same_row_left_to_right(self):
        rows = group_rows([line("right", 300, 100, h=12), line("left", 10, 102, h=12), line("below", 10, 130, h=12),
                           line("close but own row", 10, 112, h=12)])
        assert rows == ["left | right", "close but own row", "below"]

    def test_on_screen_rows_are_capped_with_a_remainder_note(self, tmp_path):
        v = visual([vstate(1, 0, 40, kind="initial"), vstate(2, 40, 50)])  # state 2 is empty, so no line is chrome
        o = ocr([ostate(1, [line(f"Row {i}", 10, 10 + 20 * i, h=10) for i in range(75)]), ostate(2)])
        make_frames(tmp_path, v)
        t = transcript([segment(0, 1, 2)])
        md = render_report(build(v, t, o), v, o, t, __version__)
        assert "- … 15 more" in md and "Row 59" in md and "Row 60" not in md.split("### On screen")[1].split("### Timeline")[0]

    def test_vocabulary_drops_numbers_single_characters_long_lines_and_chrome(self):
        common = [line("Loans", 5, 5), line("Home", 5, 30), line("42", 5, 60), line("X", 5, 90),
                  line("one two three four five six seven", 5, 120), line("12 %", 5, 150)]
        states = [ostate(i, common + ([line("Rare term", 5, 200)] if i == 1 else [])) for i in range(1, 6)]
        vocab = vocabulary(ocr(states), {"home"})
        assert vocab == ["Loans", "Rare term"]

    def test_pointer_and_screen_lines_in_the_markdown(self, tmp_path):
        states = [vstate(1, 0, 10, kind="initial"), vstate(2, 10, 20, kind="scroll", dy=-100, cursor=[CursorSample(t=14, x=100, y=105, confidence=0.5)]),
                  vstate(3, 20, 25, kind="minor"), vstate(4, 25, 40, kind="partial")]
        sc = OcrScroll(visual_dy=-100, ocr_dy=-99, matched_lines=9, consistent_lines=9)
        o = ocr([ostate(1, [line("Title text", 10, 10, h=24)]), ostate(2, [line("Save", 90, 100, w=60, h=12)], scroll=sc),
                 ostate(3), ostate(4, [line("New row", 10, 300)])])
        t = transcript([segment(0, 1, 5, "welcome"), segment(1, 12, 16, "now click this button")])
        make_frames(tmp_path, visual(states))
        md = render_report(build(visual(states), t, o), visual(states), o, t, __version__)
        assert "**SCREEN [00:00:10]** scrolled down about 100 px ([s0002](frames/s0002_t00-00-00.jpg))" in md
        # a collapsed run of minor changes links each of its frames
        assert ("**SCREEN [00:00:20–00:00:25]** 1 small update without new text (animation or counters): "
                "[s0003](frames/s0003_t00-00-00.jpg)") in md
        assert md.index("**SAID [00:00:12]** now click this button") < md.index('**POINTER [00:00:14]** pointer near "Save"')
        assert md.count("**POINTER") == 1


def test_analysis_prompt_is_the_agreed_text():
    assert PROMPT.startswith("# How to analyse this recording\n\nYou have `report.md`")
    assert PROMPT.endswith("report the final view and note the change.\n")
    assert "Findings table" in PROMPT and "Open questions" in PROMPT


# ---- the stage ---------------------------------------------------------------------------------------------


def stage_statuses(rec) -> dict[str, str]:
    return {s.stage: s.status for s in rec.stages}


@pytest.fixture
def srt(tmp_path) -> Path:
    f = tmp_path / "talk.srt"
    f.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello there, click this.\n\n2\n00:00:01,500 --> 00:00:02,500\nSecond cue.\n")
    return f


def test_stage_writes_three_files_and_is_cached(letterbox_video, srt, tmp_path):
    out = tmp_path / "out"
    cfg = Config(transcript=srt, layout_samples=4)
    rec, run_dir = run_pipeline(letterbox_video, out, cfg)
    assert stage_statuses(rec)["report"] == "miss"
    for name in ("report.md", "timeline.json", "ANALYSIS_PROMPT.md", "work/timeline.json"):
        assert (run_dir / name).is_file(), name
    assert (run_dir / "ANALYSIS_PROMPT.md").read_text() == PROMPT
    tl = TimelineArtifact.model_validate_json((run_dir / "timeline.json").read_text())
    assert tl.steps and sum(len(s.segment_ids) for s in tl.steps) == 2
    md = (run_dir / "report.md").read_text()
    for target in re.findall(r"\]\(([^)#][^)]*)\)", md):
        assert (run_dir / unquote(target)).is_file(), target

    outputs = ["report.md", "timeline.json", "ANALYSIS_PROMPT.md", "transcript.md"]
    outputs += [str(p.relative_to(run_dir)) for p in (run_dir / "work").iterdir() if p.is_file()]
    outputs += [str(p.relative_to(run_dir)) for p in (run_dir / "frames").iterdir()]
    mtimes = {name: (run_dir / name).stat().st_mtime_ns for name in outputs}
    rec2, _ = run_pipeline(letterbox_video, out, cfg)
    assert set(stage_statuses(rec2).values()) == {"hit"}
    # a run that changes nothing leaves every output file alone (transcript.md was rewritten on every run)
    assert {name: (run_dir / name).stat().st_mtime_ns for name in outputs} == mtimes

    (run_dir / "report.md").unlink()
    rec3, _ = run_pipeline(letterbox_video, out, cfg)
    assert stage_statuses(rec3) == {"ingest": "hit", "asr": "hit", "visual": "hit", "ocr": "hit", "report": "miss"}
    assert (run_dir / "report.md").is_file()

    rec4, _ = run_pipeline(letterbox_video, out, replace(cfg, step_min_seconds=10.0))
    assert stage_statuses(rec4) == {"ingest": "hit", "asr": "hit", "visual": "hit", "ocr": "hit", "report": "miss"}


def test_stage_declares_its_config_and_dependencies():
    assert REPORT_STAGE.deps == ("asr", "visual", "ocr")
    assert set(REPORT_STAGE.config_fields) == {"step_min_seconds", "step_max_seconds", "step_extra_frames"}
    assert set(REPORT_STAGE.outputs(None)) == {"../report.md", "../timeline.json", "../ANALYSIS_PROMPT.md"}
