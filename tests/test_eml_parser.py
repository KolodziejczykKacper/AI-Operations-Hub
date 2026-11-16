import pytest

from ai_ops_hub.errors import EmptyInputError, InputTooLargeError, MalformedMessageError
from ai_ops_hub.parsing import parse_eml
from ai_ops_hub.schemas import SourceKind

from conftest import read_sample


def test_polish_utf8_message(settings):
    m = parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings)
    assert m.source_kind is SourceKind.EML
    assert m.from_addr == "anna.wisniewska@zielonadolina.example"
    assert m.to_addrs == ("ksiegowosc@fabryka-przykladow.example",)
    assert "12 500,00 zł" in m.body_text
    assert "Dzień dobry" in m.body_text
    assert m.missing_headers == ()
    assert m.date is not None
    # domain lowercased, local part preserved
    assert m.message_id == "inv-2026-0042@zielonadolina.example"
    assert m.message_id_raw == "<inv-2026-0042@Zielonadolina.example>"


def test_english_multiple_recipients(settings):
    m = parse_eml(read_sample("03_contract_renewal_en.eml"), settings)
    assert len(m.to_addrs) == 2
    assert "EUR 8,400.00" in m.body_text


def test_multipart_prefers_plain_and_decodes_encoded_subject(settings):
    m = parse_eml(read_sample("04_delivery_delay_mixed_multipart.eml"), settings)
    assert "HTML version" not in m.body_text
    assert "PO-7731" in m.body_text
    assert m.subject == "Opóźnienie dostawy / Delivery delay"


def test_html_only_is_converted_and_flagged(settings):
    m = parse_eml(read_sample("05_html_only_en.eml"), settings)
    assert "USD 3,250.00" in m.body_text
    assert "<" not in m.body_text
    assert "var x" not in m.body_text and "color: red" not in m.body_text
    assert any("HTML" in w for w in m.warnings)


def test_legacy_windows_1250_decoded(settings):
    m = parse_eml(read_sample("06_legacy_cp1250_pl.eml"), settings)
    assert "Szanowni Państwo" in m.body_text
    assert "4 200,00 zł" in m.body_text
    assert "poważaniem" in m.body_text
    assert m.subject == "Reklamacja zamówienia ZAM-1180"


def test_unknown_charset_falls_back_with_warning(settings):
    m = parse_eml(read_sample("07_unknown_charset.eml"), settings)
    assert "all shipments are on schedule" in m.body_text
    assert any("fallback" in w for w in m.warnings)


def test_missing_headers_are_reported_not_fatal(settings):
    m = parse_eml(read_sample("08_no_message_id_no_date.eml"), settings)
    assert set(m.missing_headers) == {"message-id", "to", "date"}
    assert m.message_id is None
    assert m.date is None
    assert "Proszę o kontakt" in m.body_text


def test_undeclared_charset_non_utf8_bytes_do_not_crash(settings):
    raw = b"From: a@b.example\nMessage-ID: <u@y.example>\n\nPrzesy\xb3ka \xbf"  # cp1250 bytes
    m = parse_eml(raw, settings)
    assert m.body_text.startswith("Przesy")
    assert any("not valid utf-8" in w for w in m.warnings)


def test_crlf_and_lf_produce_same_body(settings):
    lf = read_sample("03_contract_renewal_en.eml")
    crlf = lf.replace(b"\n", b"\r\n")
    assert parse_eml(lf, settings).body_text == parse_eml(crlf, settings).body_text


def test_raw_hash_is_of_original_bytes(settings):
    import hashlib

    data = read_sample("03_contract_renewal_en.eml")
    assert parse_eml(data, settings).raw_sha256 == hashlib.sha256(data).hexdigest()


def test_unparseable_date_warns(settings):
    raw = b"From: a@b.example\nDate: not a date\nMessage-ID: <x@y.example>\n\nbody"
    m = parse_eml(raw, settings)
    assert m.date is None and m.date_raw == "not a date"
    assert any("Date" in w for w in m.warnings)


def test_duplicate_message_id_headers_warn(settings):
    raw = b"From: a@b.example\nMessage-ID: <one@y.example>\nMessage-ID: <two@y.example>\n\nbody"
    m = parse_eml(raw, settings)
    assert m.message_id == "one@y.example"
    assert any("multiple Message-ID" in w for w in m.warnings)


def test_bad_base64_body_does_not_crash(settings):
    raw = (
        b"From: a@b.example\nMessage-ID: <b64@y.example>\n"
        b"Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n\n"
        b"!!!not-valid-base64!!!\n"
    )
    m = parse_eml(raw, settings)  # must not raise
    assert isinstance(m.body_text, str)


def test_invalid_utf8_bytes_declared_utf8_do_not_crash(settings):
    raw = (
        b"From: a@b.example\nMessage-ID: <bad@y.example>\n"
        b"Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: 8bit\n\n"
        b"Z\xc5\xbc\xff\xfe broken"
    )
    m = parse_eml(raw, settings)
    assert "broken" in m.body_text


def test_attachment_only_message_has_empty_body_warning(settings):
    raw = (
        b"From: a@b.example\nMessage-ID: <att@y.example>\n"
        b"Content-Type: application/pdf\nContent-Transfer-Encoding: base64\n\nJVBERi0=\n"
    )
    m = parse_eml(raw, settings)
    assert m.body_text == ""
    assert any("no text body" in w for w in m.warnings)


def test_null_bytes_removed_from_body(settings):
    raw = b"From: a@b.example\nMessage-ID: <n@y.example>\n\nhel\x00lo"
    assert parse_eml(raw, settings).body_text == "hello"


# --- error / boundary cases -------------------------------------------------


@pytest.mark.parametrize("data", [b"", b"   \n\r\n  "])
def test_empty_input_rejected(data, settings):
    with pytest.raises(EmptyInputError):
        parse_eml(data, settings)


def test_non_bytes_rejected(settings):
    with pytest.raises(TypeError):
        parse_eml("From: a@b.example\n\nbody", settings)  # type: ignore[arg-type]


def test_garbage_without_headers_rejected(settings):
    with pytest.raises(MalformedMessageError):
        parse_eml(b"\x00\x01\x02 this is not an email at all", settings)


def test_oversized_eml_generated_dynamically(small_settings):
    body = b"x" * (small_settings.max_eml_bytes + 1)
    with pytest.raises(InputTooLargeError):
        parse_eml(b"From: a@b.example\n\n" + body, small_settings)


def test_exactly_at_size_limit_is_accepted(small_settings):
    head = b"From: a@b.example\n\n"
    data = head + b"x" * (small_settings.max_eml_bytes - len(head))
    assert len(data) == small_settings.max_eml_bytes
    assert parse_eml(data, small_settings).body_text.startswith("xxx")


def test_too_many_headers_rejected(small_settings):
    headers = b"".join(b"X-H%d: v\n" % i for i in range(small_settings.max_headers + 1))
    with pytest.raises(InputTooLargeError):
        parse_eml(b"From: a@b.example\n" + headers + b"\nbody", small_settings)


def test_too_many_mime_parts_rejected(small_settings):
    parts = b"".join(
        b"--B\nContent-Type: text/plain\n\npart %d\n" % i
        for i in range(small_settings.max_mime_parts + 2)
    )
    raw = b"From: a@b.example\nContent-Type: multipart/mixed; boundary=B\n\n" + parts + b"--B--\n"
    with pytest.raises(InputTooLargeError):
        parse_eml(raw, small_settings)


def test_deeply_nested_mime_does_not_crash(settings):
    depth = 300
    lines = [b"From: a@b.example", b"Content-Type: multipart/mixed; boundary=b0", b""]
    for i in range(depth):
        lines += [
            b"--b%d" % i,
            b"Content-Type: multipart/mixed; boundary=b%d" % (i + 1),
            b"",
        ]
    with pytest.raises((MalformedMessageError, InputTooLargeError)):
        parse_eml(b"\n".join(lines), settings)
