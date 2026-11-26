"""Text that framescribe did not write, in its Markdown documents and on the terminal, rendered with cmark-gfm (GitHub's
own Markdown renderer, raw HTML allowed as a non-sanitising viewer would show it): every string field of the artifacts
set to a hostile payload, an imported transcript JSON with hostile metadata or fill flags, block markers at the start
of a line, and autolinks in any case. Only example.com style names. Hidden format characters: test_invisible.py."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import cmarkgfm
import pytest
from cmarkgfm.cmark import Options
from pydantic import BaseModel
from report_synth import line, ocr, ostate, visual, vstate
from typer.testing import CliRunner

from framescribe.cli import app
from framescribe.errors import StageError
from framescribe.mdtext import Md, document, marked_text, md, md_inline, md_join, md_text, own
from framescribe.models import (
    Completeness,
    Gap,
    IngestArtifact,
    TimelineArtifact,
    TranscriptArtifact,
    TranscriptSegment,
    TranscriptWord,
)
from framescribe.render import render_transcript
from framescribe.report_md import render_report
from framescribe.stages.asr_import import import_transcript
from framescribe.steps import build_steps, find_chrome
from framescribe.timeline import build_timeline

runner = CliRunner()

INLINE = ("[x](https://p.example/a) <img src=x onerror=alert(1)> ![i](https://p.example/i.png) www.p.example "
          "HTTPS://q.example Mailto:bob@p.example WWW.r.example <b>b</b> *e* _u_ ~~s~~ `c` | cell | [ref]: /x &amp;")
BLOCKS = ["1. ordered", "2) ordered", "- item", "+ item", "* item", "> quote", "# heading", "###### heading", "---",
          "***", "___", "===", "    indented code", "\tindented code", "```fence", "~~~fence", "<div>html</div>",
          "| a | b |", "- [ ] task", "[^1]: note", "  - spaced item", "123456789. big number"]


def gfm(md_doc: str) -> str:
    return cmarkgfm.github_flavored_markdown_to_html(md_doc, options=Options.CMARK_OPT_UNSAFE)


class _Elements(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.code: list[str] = []
        self._in_code = False
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._in_code = tag == "code"
        if tag == "code":
            self.code.append("")

    def handle_endtag(self, tag):
        if tag == "code":
            self._in_code = False

    def handle_data(self, data):
        self.text.append(data)
        if self._in_code:
            self.code[-1] += data


TOOL_TAGS = {"h1", "h2", "h3", "p", "ul", "li", "table", "thead", "tbody", "tr", "th", "td", "strong", "code", "img", "a"}


def elements(md_doc: str) -> _Elements:
    parser = _Elements()
    parser.feed(gfm(md_doc))
    return parser


def foreign(md_doc: str, baseline: str) -> list[str]:
    """What the rendered `md_doc` has that the tool does not write: an element of another kind, a link or image to
    anything but a relative path or an anchor, a heading, table or row more than in `baseline`, a code span the
    baseline has not got (the tool's marks and notes are the only code spans)."""
    got, base = elements(md_doc), elements(baseline)
    found = [f"<{t}>" for t, _ in got.tags if t not in TOOL_TAGS]
    for t, attrs in got.tags:
        target = attrs.get("href") if t == "a" else attrs.get("src") if t == "img" else None
        if target is not None and (re.match(r"[A-Za-z][A-Za-z0-9+.-]*:|/", target) or not target):
            found.append(f"<{t} {target}>")
    for kind in ("h1", "h2", "h3", "table", "tr", "ul"):
        n, m = sum(t == kind for t, _ in got.tags), sum(t == kind for t, _ in base.tags)
        if n != m:
            found.append(f"{n} <{kind}> where the tool writes {m}")
    found += [f"code span {c!r}" for c in set(got.code) - set(base.code)]
    return found


# ---- the artifacts of a small cross-checked run --------------------------------------------------------------------


def run_artifacts() -> dict[str, Any]:
    v = visual([vstate(1, 0, 40, kind="initial"), vstate(2, 40, 80, kind="page", conf=1.0)])
    o = ocr([ostate(1, [line("Welcome page", 10, 10, h=30), line("Sign in", 10, 60)]),
             ostate(2, [line("Settings", 10, 10, h=30), line("Save changes", 10, 60)])])
    words = [TranscriptWord(text="we", start=1.0, end=1.4), TranscriptWord(text="open", start=1.5, end=1.9),
             TranscriptWord(text="settings", start=2.0, end=2.4, filled=True, confirmed=True, source="whisperkit"),
             TranscriptWord(text="now", start=41.0, end=41.4),
             TranscriptWord(text="maybe", start=41.5, end=41.9, filled=True, confirmed=False, source="captions")]
    segs = [TranscriptSegment(id=1, start=1.0, end=2.4, text=marked_text(words[:3]), speaker="Ann", words=words[:3]),
            TranscriptSegment(id=2, start=41.0, end=41.9, text=marked_text(words[3:]), speaker="Bo", words=words[3:])]
    c = Completeness(primary="parakeet", primary_words=3, sources=["parakeet-pass2", "whisperkit", "captions"],
                     unavailable=["second pass: none"], filled_words=2, filled_by_source={"whisperkit": 1, "captions": 1},
                     filled_share=0.4, confirmed=1, unconfirmed=1, rejected=1, rejected_by_reason={"fragment": 1},
                     gaps=[Gap(start=2.0, end=2.4, text="settings", words=1, sources=["whisperkit", "captions"],
                               chosen="whisperkit", status="confirmed")],
                     limit=0.03, above_limit=True, warning="2 of the transcript's 5 words were filled in")
    t = TranscriptArtifact(primary="parakeet", segments=segs, sources=["parakeet", "whisperkit"], completeness=c,
                           alternates={"whisperkit": []})
    ingest = IngestArtifact.model_validate({
        "source": "/m/talk.mp4", "duration": 80, "video": {"width": 2, "height": 2, "fps": 1, "codec": "h264"},
        "audio": None, "audio_wav": None, "embedded_captions": [{"start": 1, "end": 2, "speaker": "Ann", "text": "hi"}],
        "layout": {"frame_w": 2, "frame_h": 2, "content_rect": {"x": 0, "y": 0, "w": 2, "h": 2}, "method": "m",
                   "confidence": 1, "warning": "w"}})
    return {"ingest": ingest, "transcript": t, "visual": v, "ocr": o}


def timeline_of(a: dict[str, Any]) -> TimelineArtifact:
    chrome = find_chrome(a["ocr"])
    plans = build_steps(a["visual"], a["transcript"], a["ocr"], chrome, 30, 240, 3)
    return build_timeline(Path(a["ingest"].source).name, "parakeet", a["visual"], a["transcript"], a["ocr"], plans,
                          chrome, 30, 240)


def render_all(a: dict[str, Any]) -> dict[str, str]:
    tl = a.get("timeline") or timeline_of(a)
    return {"report.md": render_report(tl, a["visual"], a["ocr"], a["transcript"], "0.0"),
            "transcript.md": render_transcript(a["ingest"], a["transcript"])}


# ---- walking every string field --------------------------------------------------------------------------------------

Step = tuple[str, Any]  # ("attr", name) | ("item", index) | ("key", key)


def _is_literal(annotation: Any) -> bool:
    if get_origin(annotation) is Literal:
        return True
    if get_origin(annotation) in (Union,) or type(annotation).__name__ == "UnionType":
        return any(_is_literal(x) for x in get_args(annotation))
    return False


def string_fields(obj: Any, path: tuple[Step, ...] = ()) -> Iterator[tuple[Step, ...]]:
    """Every place in `obj` that holds a string, dict keys included. Literal fields (a fixed set of words that
    validation enforces, such as an event's type) are left out: they are never text."""
    if isinstance(obj, BaseModel):
        for name, info in type(obj).model_fields.items():
            if _is_literal(info.annotation):
                continue
            value = getattr(obj, name)
            if isinstance(value, str):
                yield (*path, ("attr", name))
            else:
                yield from string_fields(value, (*path, ("attr", name)))
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            if isinstance(x, str):
                yield (*path, ("item", i))
            else:
                yield from string_fields(x, (*path, ("item", i)))
    elif isinstance(obj, dict):
        for k, x in obj.items():
            yield (*path, ("key", k))
            if isinstance(x, str):
                yield (*path, ("item", k))
            else:
                yield from string_fields(x, (*path, ("item", k)))


def put(root: Any, path: tuple[Step, ...], payload: str) -> None:
    """Set the string at `path` to `payload`, past validation (as a tampered cached artifact would hold it)."""
    *head, (kind, at) = path
    obj = root
    for k, x in head:
        obj = getattr(obj, x) if k == "attr" else obj[x]
    if kind == "attr":
        object.__setattr__(obj, at, payload) if isinstance(obj, BaseModel) else setattr(obj, at, payload)
    elif kind == "item":
        obj[at] = payload
    else:  # a dict key
        items = list(obj.items())
        obj.clear()
        obj.update({payload if k == at else k: v for k, v in items})


def every_field() -> list[tuple[str, tuple[Step, ...]]]:
    a = run_artifacts()
    a["timeline"] = timeline_of(a)
    return [(name, p) for name, art in a.items() for p in string_fields(art)]


FIELDS = every_field()


def test_the_walk_reaches_the_fields_the_review_named():
    paths = {(name, ".".join(str(x) for _, x in p)) for name, p in FIELDS}
    for want in [("transcript", "primary"), ("transcript", "completeness.primary"),
                 ("transcript", "completeness.sources.0"), ("transcript", "completeness.filled_by_source.whisperkit"),
                 ("transcript", "completeness.rejected_by_reason.fragment"), ("transcript", "completeness.warning"),
                 ("transcript", "segments.0.speaker"), ("transcript", "segments.0.words.2.source"),
                 ("timeline", "asr_backend"), ("timeline", "source"), ("ingest", "source"), ("ocr", "states.0.lines.0.text"),
                 ("visual", "states.0.keyframe_path")]:
        assert want in paths, want
    assert len(FIELDS) > 60


@pytest.mark.parametrize(("artifact", "path"), FIELDS, ids=[f"{n}." + ".".join(str(x) for _, x in p) for n, p in FIELDS])
def test_a_hostile_value_in_any_string_field_stays_text_in_every_markdown_output(artifact, path):
    baseline = render_all(run_artifacts())
    for payload in (INLINE, *BLOCKS):
        a = run_artifacts()
        if artifact == "timeline":
            a["timeline"] = timeline_of(a)
        put(a[artifact], path, payload)
        if ("attr", "words") in path:  # a word changed: the segment's text as the asr stage makes it from its words
            for seg in a["transcript"].segments:
                object.__setattr__(seg, "text", marked_text(seg.words))
        docs = render_all(a)
        for name, doc in docs.items():
            assert INLINE not in doc, (name, path)  # never raw
            assert foreign(doc, baseline[name]) == [], (name, path, payload, foreign(doc, baseline[name]))


def test_the_baseline_renders_the_tools_own_structure():
    docs = render_all(run_artifacts())
    report = elements(docs["report.md"])
    assert "[+" in report.code and "?]" in report.code  # the tool's marks are there to compare against
    assert any(t == "img" and a["src"].startswith("frames/") for t, a in report.tags)


# ---- the boundary itself -----------------------------------------------------------------------------------------------


def test_a_document_is_made_of_md_only_and_md_escapes_every_plain_value():
    with pytest.raises(TypeError, match="not Md"):
        document([own("# Title"), f"Source: {INLINE}"])  # a future f-string with a field in it is refused
    assert isinstance(md("a {}", "b"), Md) and not isinstance(own("a") + "b", Md)  # adding gives a plain str
    assert md("Source: {}.", "<b>x</b>") == "Source: &lt;b&gt;x&lt;/b&gt;."
    assert md("{} and {}", own("**own**"), "**theirs**") == "**own** and \\*\\*theirs\\*\\*"
    assert md_join(", ", ["[a](https://p.example)", own("`b`")]) == "\\[a\\]\\(https\\://p.example\\), `b`"
    assert md("{} words", 12) == "12 words"
    with pytest.raises(TypeError):
        own(3)  # type: ignore[arg-type]


def test_the_completeness_line_escapes_every_name_and_number_it_holds():
    from framescribe.mdtext import MARKDOWN

    c = run_artifacts()["transcript"].completeness
    object.__setattr__(c, "filled_by_source", {"![i](https://p.example/i.png)": 2})
    object.__setattr__(c, "rejected_by_reason", {"<a href=https://p.example>r</a>": 1})
    object.__setattr__(c, "sources", ["[s](https://p.example)"])
    line_md = c.summary(MARKDOWN)
    assert isinstance(line_md, Md)
    assert "\\!\\[i\\]\\(https\\://p.example/i.png\\)" in line_md and "&lt;a href=https\\://p.example&gt;" in line_md
    assert "\\[s\\]\\(https\\://p.example\\)" in line_md
    assert c.summary() == ("cross-checked against [s](https://p.example); filled 2 words (40.0%; 2 from "
                           "![i](https://p.example/i.png)): 1 confirmed, 1 unconfirmed, 1 rejected gaps (1 <a "
                           "href=https://p.example>r</a>); ABOVE THE USUAL 3.0%: check the filled-in words")  # plain: as is


def test_a_link_target_never_leaves_the_folder():
    from framescribe.report_md import link

    assert link("frames/s0001_t00-00-00.jpg") == "frames/s0001_t00-00-00.jpg"
    assert link("//evil.example/x.png") == ".//evil.example/x.png"
    assert link("https://evil.example/x") == "https%3A//evil.example/x"
    assert link("/etc/passwd") == "./etc/passwd"


# ---- the verifier's payload: an imported transcript JSON with hostile metadata ------------------------------------------

EVIL_META = {
    "schema_version": 3,
    "primary": "[click](https://primary.example.com) <img src=x onerror=alert(1)> www.primary.example.com",
    "segments": [{"id": 1, "start": 0.5, "end": 2.1, "text": "recorded [+ real fill +] end", "speaker": "Bob",
                  "words": [{"text": "recorded", "start": 0.5, "end": 0.7},
                            {"text": "real", "start": 1.5, "end": 1.7, "source": "parakeet-pass2", "filled": True,
                             "confirmed": True},
                            {"text": "fill", "start": 1.7, "end": 1.9, "source": "parakeet-pass2", "filled": True,
                             "confirmed": True},
                            {"text": "end", "start": 1.9, "end": 2.1}]}],
    "alternates": {},
    "sources": ["parakeet", "parakeet-pass2", "captions"],
    "completeness": {"primary": "[cp](https://cprimary.example.com)", "primary_words": 2,
                     "sources": ["[src](https://sources.example.com)", "<b>bold</b> www.src.example.com"],
                     "unavailable": [], "filled_words": 2, "filled_by_source": {"![img](https://img.example.com/i.png)": 2},
                     "filled_share": 0.5, "confirmed": 1, "unconfirmed": 0, "rejected": 0,
                     "rejected_by_reason": {"<a href=https://reason.example.com>r</a>": 1}, "gaps": []},
}


def clean_meta() -> dict:
    d = json.loads(json.dumps(EVIL_META))
    d["primary"] = "parakeet"
    d["completeness"].update(primary="parakeet", sources=["parakeet-pass2", "captions"],
                             filled_by_source={"parakeet-pass2": 2}, rejected_by_reason={"fragment": 1})
    return d


@pytest.mark.parametrize("where", ["primary", "completeness.primary", "completeness.sources",
                                   "completeness.filled_by_source", "completeness.rejected_by_reason", "word source",
                                   "alternates", "sources"])
def test_an_imported_transcript_json_with_a_name_framescribe_does_not_write_is_refused(tmp_path, where):
    d = clean_meta()
    bad = "[x](https://p.example.com)"
    if where == "word source":
        d["segments"][0]["words"][1]["source"] = bad
    elif where == "alternates":
        d["alternates"] = {bad: []}
    elif where == "sources":
        d["sources"] = [bad]
    elif "." in where:
        key = where.split(".")[1]
        old = d["completeness"][key]
        d["completeness"][key] = bad if isinstance(old, str) else [bad] if isinstance(old, list) else {bad: 1}
    else:
        d[where] = bad
    f = tmp_path / "t.json"
    f.write_text(json.dumps(d))
    with pytest.raises(StageError, match="not a source or reason name framescribe writes") as e:
        import_transcript(f)
    assert "is not a framescribe transcript JSON" in str(e.value)


def test_the_verifiers_payload_is_refused_on_the_command_line(letterbox_video, tmp_path):
    f = tmp_path / "evilmeta.json"
    f.write_text(json.dumps(EVIL_META))
    res = runner.invoke(app, ["run", str(letterbox_video), "--out", str(tmp_path / "o"), "--transcript", str(f),
                              "--progress", "plain"])
    assert res.exit_code == 2, res.output
    assert "is not a framescribe transcript JSON" in res.output and "not a source or reason name" in res.output
    assert "Traceback" not in res.output
    assert not any(p.name in ("report.md", "transcript.md") for p in (tmp_path / "o").rglob("*"))


def test_fill_flags_of_an_imported_transcript_never_become_the_tools_marks(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps(clean_meta()))
    t = import_transcript(f)
    assert t.completeness is None and not any(w.filled or w.confirmed or w.source for s in t.segments for w in s.words)
    a = run_artifacts()
    a["transcript"] = t
    docs = render_all(a)
    for name, doc in docs.items():
        said = [x for x in doc.splitlines() if x.startswith(("**[", "- **SAID"))]
        assert said and all("`" not in x for x in said), said  # no code span: no mark of the tool's
        assert any("\\[+ real fill +\\]" in x for x in said)  # the file's brackets, as text
        assert "imported" in doc and "bracketed text that looks like one is part of the imported text" in doc
        assert foreign(doc, render_all(run_artifacts())[name]) == []


def test_the_tools_own_transcript_json_round_trips_without_its_marks(tmp_path):
    t = run_artifacts()["transcript"]
    f = tmp_path / "transcript.json"
    f.write_text(t.model_dump_json(indent=1))
    back = import_transcript(f)
    assert back.primary == t.primary and back.sources == t.sources and back.completeness is None
    assert [(s.id, s.start, s.end, s.text, s.speaker) for s in back.segments] == \
        [(s.id, s.start, s.end, s.text, s.speaker) for s in t.segments]
    assert [(w.text, w.start, w.end) for s in back.segments for w in s.words] == \
        [(w.text, w.start, w.end) for s in t.segments for w in s.words]
    tmd = render_transcript(run_artifacts()["ingest"], back)
    assert "\\[+ settings +\\]" in tmd and "`[+` settings `+]`" not in tmd
    assert "not cross-checked" in tmd


# ---- block markers at the start of a line or list item -------------------------------------------------------------------


def test_recorded_text_at_the_start_of_a_line_or_list_item_starts_no_block():
    v = visual([vstate(1, 0, 40, kind="initial"), vstate(2, 40, 80, kind="page", conf=1.0),
                vstate(3, 80, 120, kind="page", conf=1.0)])
    o = ocr([ostate(1, [line("first", 10, 10, h=30)]),
             ostate(2, [line("Title", 10, 10, h=30)] + [line(b + " x", 10, 60 + 30 * i) for i, b in enumerate(BLOCKS)]),
             ostate(3, [line("third", 10, 10, h=30)])])
    segs = [TranscriptSegment(id=i + 1, start=41 + i, end=41.5 + i, text=b + " said") for i, b in enumerate(BLOCKS)]
    t = TranscriptArtifact(primary="import", sources=["import:t.srt"], segments=segs)
    a = {**run_artifacts(), "visual": v, "ocr": o, "transcript": t}
    docs = render_all(a)
    for name, doc in docs.items():
        got = elements(doc)
        kinds = {t for t, _ in got.tags}
        assert kinds <= TOOL_TAGS, (name, kinds - TOOL_TAGS)
        html = gfm(doc)
        assert "<li>\n<ul>" not in html and "<li>\n<ol>" not in html  # no nested list in a list item
        assert "<ol" not in html and "<hr" not in html and "<blockquote" not in html and "<pre" not in html
        headings = re.findall(r"<h[1-6]>(.*?)</h[1-6]>", html)
        assert all(h.startswith(("Transcript: ", "Evidence report: ", "How to read", "On-screen vocabulary", "Steps",
                                 "Step ", "On screen", "Other frames", "Timeline", "Coverage")) for h in headings), headings
        text = "".join(got.text)
        for b in ("1. ordered", "> quote", "---", "===", "```fence"):
            assert b in text, (name, b)  # still there, as text
    report = docs["report.md"]
    assert "- 1\\. ordered x" in report and "- \\- item x" in report and "- \\+ item x" in report
    assert "- indented code x" in report and md_text("---") == "\\---" and md_text("= = =") == "\\= = ="
    assert "**[00:00:41]** 1\\. ordered said" in docs["transcript.md"]


@pytest.mark.parametrize("text", BLOCKS)
def test_md_text_alone_on_a_line_or_in_a_list_item_is_a_paragraph(text):
    for doc in (md_text(text), f"- {md_text(text)}", f"Before\n{md_text(text)}"):
        html = gfm(doc)
        assert not re.search(r"<(ol|hr|blockquote|pre|h[1-6]|table|div|input)\b", html), (doc, html)
        assert html.count("<li>") <= 1 and "<ul>\n<li>\n<ul>" not in html, (doc, html)


def test_numbers_and_dashes_inside_text_are_left_alone():
    assert md_text("1.5 s and 2. later") == "1.5 s and 2. later"
    assert md_text("-5 dB") == "-5 dB" and md_text("--flag") == "--flag" and md_text("a - b") == "a - b"
    assert md_text("2024. A year") == "2024\\. A year"  # at the start it could be a list


# ---- autolinks in any case ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "escaped"), [
    ("HTTPS://evil.example/x", "HTTPS\\://evil.example/x"),
    ("Mailto:bob", "Mailto\\:bob"),
    ("MAILTO:bob", "MAILTO\\:bob"),
    ("Xmpp:bob", "Xmpp\\:bob"),
    ("WWW.evil.example", "WWW\\.evil.example"),
    ("Www.evil.example", "Www\\.evil.example"),
])
def test_schemes_and_www_in_upper_or_mixed_case_are_neutralised(text, escaped):
    assert md_inline(text) == escaped  # fails without re.IGNORECASE in mdtext
    assert "<a " not in gfm(md_text(text))


def test_mixed_case_addresses_render_without_a_link():
    for text in ("HTTPS://EVIL.EXAMPLE/x", "Mailto:bob@evil.example", "WWW.evil.example", "Xmpp:bob@evil.example"):
        html = gfm(md_text(text))
        assert "<a " not in html, (text, html)
    # cmark-gfm itself links these two raw (it leaves WWW. in capitals alone; other viewers need not)
    assert all("<a " in gfm(raw) for raw in ("HTTPS://EVIL.EXAMPLE/x", "Mailto:bob@evil.example"))


def test_a_bare_domain_is_left_as_text():
    """GFM does not link example.com/x without a scheme or www.; viewers that do (linkify) are not GFM, and the text is
    not changed for them (README, SECURITY.md)."""
    assert md_text("example.com/x") == "example.com/x"
    assert "<a " not in gfm(md_text("example.com/x"))
