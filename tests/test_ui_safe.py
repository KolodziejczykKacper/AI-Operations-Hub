"""Helpers for showing untrusted text."""

from __future__ import annotations

import pytest

from ai_ops_hub.ui.safe import clip, context_snippet, esc

PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "![pixel](http://evil.example/p.png)",
    "[click me](javascript:alert(1))",
    "<a href='http://evil.example'>x</a>",
    "http://evil.example/auto-link",
    ":red[colored] :smile: **bold** _italic_ `code`",
    "# heading\n- list\n> quote",
    "$$ \\text{latex} $$",
    "| a | b |\n|---|---|",
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_escaped_text_has_no_active_markdown(payload):
    out = esc(payload)
    for danger in ("<", ">", "[", "]", "(", ")", "*", "_", "`", "#", "|", "$", ":", "!"):
        for i, ch in enumerate(out):
            if ch == danger:
                assert out[i - 1] == "\\", f"unescaped {danger!r} in {out!r}"


def test_esc_handles_none_and_non_strings():
    assert esc(None) == ""
    assert esc(42) == "42"


def test_esc_removes_control_characters():
    assert esc("a\x00b\x07c") == "abc"


def test_esc_keeps_plain_words_readable():
    assert esc("Pump Alpha") == "Pump Alpha"


def test_clip():
    assert clip("a  b\n c") == "a b c"
    assert clip("x" * 200, 10) == "xxxxxxxxx…"


def test_context_snippet_marks_the_match_and_shows_neighbours():
    body = "Please deliver the 10 pcs of Pump Alpha by 15/12 to our plant."
    start = body.index("15/12")
    out = context_snippet(body, start, start + 5, radius=12)
    assert "[[15/12]]" in out and out.startswith("…") and out.endswith("…")
    assert out.count("[[") == 1


def test_context_snippet_at_the_edges_has_no_ellipsis():
    assert context_snippet("15/12", 0, 5) == "[[15/12]]"


def test_context_snippet_collapses_newlines():
    assert "\n" not in context_snippet("line one\nline 15/12\nline three", 14, 19)


@pytest.mark.parametrize(("start", "end"), [(5, 2), (-1, 3), (0, 99), (4, 4)])
def test_context_snippet_with_bad_offsets_does_not_crash(start, end):
    assert isinstance(context_snippet("short", start, end), str)
