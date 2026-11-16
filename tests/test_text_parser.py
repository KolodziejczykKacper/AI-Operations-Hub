import pytest

from ai_ops_hub.errors import EmptyInputError, InputTooLargeError
from ai_ops_hub.parsing import parse_pasted_text
from ai_ops_hub.schemas import SourceKind

from conftest import read_sample


def test_english_headers_and_body(settings):
    m = parse_pasted_text(read_sample("09_payment_reminder_en.txt").decode("utf-8"), settings)
    assert m.source_kind is SourceKind.PASTED_TEXT
    assert m.subject == "Second payment reminder - INV-5521"
    assert m.from_addr == "laura.fenwick@northbridge-logistics.example"
    assert m.message_id == "reminder-0009@northbridge-logistics.example"
    assert m.body_text.startswith("Hi,")
    assert "USD 3,250.00" in m.body_text
    assert m.missing_headers == ()


def test_polish_header_labels(settings):
    m = parse_pasted_text(read_sample("10_reklamacja_pl.txt").decode("utf-8"), settings)
    assert m.from_addr == "ewa.nowak@zielonadolina.example"
    assert m.to_addrs == ("reklamacje@fabryka-przykladow.example",)
    assert m.subject == "Reklamacja partii PART-99"
    assert m.date is not None  # ISO 8601 accepted
    assert m.body_text.startswith("Dzień dobry")
    assert set(m.missing_headers) == {"message-id"}


def test_plain_body_without_headers(settings):
    m = parse_pasted_text("Proszę o fakturę.\nThank you.", settings)
    assert m.body_text == "Proszę o fakturę.\nThank you."
    assert set(m.missing_headers) == {"message-id", "from", "to", "subject", "date"}
    assert m.message_id is None


def test_body_line_that_looks_like_label_is_not_header_when_not_at_top(settings):
    m = parse_pasted_text("Hello there.\nSubject: this is body text", settings)
    assert m.subject is None
    assert "Subject: this is body text" in m.body_text


def test_unknown_label_at_top_is_body(settings):
    m = parse_pasted_text("Priorytet: wysoki\n\nTreść", settings)
    assert m.subject is None
    assert m.body_text.startswith("Priorytet: wysoki")


def test_folded_header_continuation(settings):
    m = parse_pasted_text("Subject: very long\n  subject line\n\nbody", settings)
    assert m.subject == "very long subject line"
    assert m.body_text == "body"


def test_header_only_text_has_empty_body_warning(settings):
    m = parse_pasted_text("Subject: only a subject\n", settings)
    assert m.body_text == ""
    assert any("empty body" in w for w in m.warnings)


def test_crlf_normalised(settings):
    a = parse_pasted_text("Subject: s\r\n\r\nline1\r\nline2", settings)
    b = parse_pasted_text("Subject: s\n\nline1\nline2", settings)
    assert a.body_text == b.body_text == "line1\nline2"


def test_nul_characters_removed_with_warning(settings):
    m = parse_pasted_text("he\x00llo", settings)
    assert m.body_text == "hello"
    assert any("NUL" in w for w in m.warnings)


def test_message_id_normalised(settings):
    m = parse_pasted_text("Message-ID: <Abc@EXAMPLE.Com>\n\nbody", settings)
    assert m.message_id == "Abc@example.com"


@pytest.mark.parametrize("text", ["", "   ", "\n\r\n\t"])
def test_empty_text_rejected(text, settings):
    with pytest.raises(EmptyInputError):
        parse_pasted_text(text, settings)


def test_non_str_rejected(settings):
    with pytest.raises(TypeError):
        parse_pasted_text(b"bytes", settings)  # type: ignore[arg-type]


def test_oversized_text_generated_dynamically(small_settings):
    with pytest.raises(InputTooLargeError):
        parse_pasted_text("a" * (small_settings.max_text_chars + 1), small_settings)


def test_exactly_at_char_limit_accepted(small_settings):
    m = parse_pasted_text("a" * small_settings.max_text_chars, small_settings)
    assert len(m.body_text) == small_settings.max_text_chars


def test_char_limit_counts_characters_not_bytes(small_settings):
    # 'ł' is 2 bytes in UTF-8 but 1 character: must still be accepted at the limit.
    m = parse_pasted_text("ł" * small_settings.max_text_chars, small_settings)
    assert len(m.body_text) == small_settings.max_text_chars
