"""Formula-injection defence for spreadsheet text (unit tests, no database)."""

from __future__ import annotations

import pytest

from ai_ops_hub.export.safe import MAX_CELL_CHARS, clean, needs_guard, safe_text

DANGEROUS = [
    "=1+1", "=HYPERLINK(\"http://evil.example\",\"x\")", "=cmd|' /C calc'!A0", "+SUM(A1:A9)", "-2+3+cmd",
    "@SUM(A1)", "|calc", "==1", "+1+1", "-1-1", "@import",
    "\t=1+1", "\r=1+1", "\n=1+1", " =1+1", "   +cmd", "\u00a0=1+1",
    "\u200b=1+1", "\ufeff=1+1", "\u202e=1+1", "\x00=1+1", "\x1f=1+1", "\u2028=1+1",
    "＝1+1", "＋cmd", "－1+1", "＠SUM(A1)",
]


@pytest.mark.parametrize("raw", DANGEROUS)
def test_formula_like_text_is_neutralised_with_a_leading_apostrophe(raw):
    out = safe_text(raw)
    assert out.startswith("'"), repr(out)
    assert not any(ord(c) < 32 or c in "\u200b\ufeff\u202e\u2028" for c in out)


@pytest.mark.parametrize("raw", DANGEROUS)
def test_multiline_text_is_neutralised_too(raw):
    assert safe_text(raw, multiline=True).startswith("'")


@pytest.mark.parametrize("raw", ["+48600100200", "-5", "+48 600 100 200", "-12,50", "10", "1.5"])
def test_plain_numbers_are_left_alone(raw):
    assert safe_text(raw) == raw


@pytest.mark.parametrize("raw", ["Hello =1+1", "a=b", "Pump Alpha", "FV/2026/0042", "anna@zielonadolina.example", "Zażółć gęślą jaźń", ""])
def test_ordinary_text_is_unchanged(raw):
    assert safe_text(raw) == raw and not needs_guard(raw)


def test_signed_number_with_trailing_formula_is_not_treated_as_a_number():
    assert safe_text("+48600100200+cmd").startswith("'")
    assert safe_text("-1+1").startswith("'")


def test_control_and_invisible_characters_are_removed_everywhere():
    assert safe_text("a\x00b\x07c\u200bd\ufeffe\u202ef") == "abcdef"
    assert safe_text("a\tb") == "a b"
    assert clean("a\x0bb") == "ab"


def test_newlines_survive_only_in_multiline_text():
    assert safe_text("a\r\nb\rc") == "a b c"
    assert safe_text("a\r\nb\rc", multiline=True) == "a\nb\nc"


def test_lone_surrogates_are_dropped_so_the_file_can_always_be_encoded():
    out = safe_text("ok\ud800bad")
    assert out == "okbad"
    out.encode("utf-8")


def test_overlong_text_is_clipped_below_the_excel_limit_and_marked():
    out = safe_text("x" * 100_000)
    assert len(out) <= MAX_CELL_CHARS < 32_767 and out.endswith("[obcięto]")


def test_none_becomes_empty_text():
    assert safe_text(None) == ""
