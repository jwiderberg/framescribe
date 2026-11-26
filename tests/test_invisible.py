"""Hidden text: every Unicode format character but the few that legitimate text needs is removed from recorded text
(fmt.strip_invisible) in Markdown, on the terminal and escaped in the JSON stream. Scripts that need joiners, emoji
sequences and the three RGI subdivision flags stay as they are."""

from __future__ import annotations

import json
import unicodedata

import pytest

from framescribe.fmt import KEPT_FORMAT, strip_invisible, terminal_text
from framescribe.mdtext import md_inline, md_text


def tags(text: str) -> str:
    return "".join(chr(0xE0000 + ord(c)) for c in text)


BLACK_FLAG, CANCEL = "\U0001f3f4", "\U000e007f"
REAL_FLAGS = [BLACK_FLAG + tags(t) + CANCEL for t in ("gbeng", "gbsct", "gbwls")]
SCRIPTS = {
    "arabic": "مرحبا بالعالم ١٢٣",
    "hebrew": "שָׁלוֹם עולם",
    "hindi": "नमस्‍ते क्‌ष हिन्दी",  # a zero-width joiner and non-joiner inside words
    "persian": "می‌خواهم کتاب‌ها",  # zero-width non-joiners
    "emoji zwj": "\U0001f468‍\U0001f469‍\U0001f467 \U0001f3f3️‍\U0001f308 \U0001f44d\U0001f3fd",
    "flags": " ".join(REAL_FLAGS) + " \U0001f1f8\U0001f1ea",
    "arabic number sign": "؀١٢٣",
}


@pytest.mark.parametrize("name", SCRIPTS)
def test_legitimate_scripts_emoji_and_the_three_subdivision_flags_stay(name):
    text = SCRIPTS[name]
    for f in (strip_invisible, terminal_text):
        assert f(text) == text, (name, f)
    assert md_text(text) == text


def test_a_chain_of_black_flags_carries_no_hidden_text():
    chain = "".join(BLACK_FLAG + tags(w) + CANCEL for w in ("ignore", "all", "instru", "ctions"))
    for f in (strip_invisible, terminal_text, md_text):
        out = f(chain)
        assert not any(0xE0000 <= ord(c) <= 0xE007F for c in out), (f, repr(out))
        assert out.count(BLACK_FLAG) == 4  # the visible flags stay
    # tags without a black flag, a flag with tags of no RGI flag, an unfinished flag: no tag survives
    for s in (tags("hidden") + CANCEL, BLACK_FLAG + tags("usca") + CANCEL, BLACK_FLAG + tags("gbeng"),
              BLACK_FLAG + tags("gbengx") + CANCEL, "\U000e0001" + tags("en")):
        assert not any(0xE0000 <= ord(c) <= 0xE007F for c in strip_invisible(s)), repr(s)
    assert strip_invisible(REAL_FLAGS[0] + tags("hidden") + CANCEL) == REAL_FLAGS[0]


@pytest.mark.parametrize("cp", [0x200B, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0x00AD, 0x180E, 0x1D173, 0x1D17A,
                                0xFEFF, 0x202E, 0x2066, 0x206A, 0xFFF9, 0xE0001])
def test_invisible_format_characters_are_removed(cp):
    text = f"a{chr(cp)}b"
    assert strip_invisible(text) == "ab" and terminal_text(text) == "ab" and md_text(text) == "ab"


def test_every_format_character_but_the_kept_ones_is_removed():
    cf = [chr(i) for i in range(0x110000) if unicodedata.category(chr(i)) == "Cf"]
    kept = {c for c in cf if c in strip_invisible("a" + c + "b")}
    assert kept == {c for c in cf if c in KEPT_FORMAT}
    assert "‌" in kept and "‍" in kept and len(kept) < 40
    assert not any(0xE0000 <= ord(c) <= 0xE007F for c in kept)


def test_the_tools_own_word_joiner_is_put_in_after_stripping():
    assert md_inline("bob⁠@evil.example") == "bob&#8288;@evil.example"  # the recorded one gone, the tool's in
    assert md_inline("a⁠b") == "ab"


def test_the_json_progress_stream_escapes_every_format_character():
    import io

    from framescribe.status import JsonRenderer

    out = io.StringIO()
    message = "x​y­z" + tags("hi") + "‍"
    JsonRenderer(out)({"event": "log", "t": 0, "stage": "asr", "message": message})
    raw = out.getvalue()
    assert raw.isascii() or not any(unicodedata.category(c) == "Cf" for c in raw)
    assert json.loads(raw)["message"] == message
