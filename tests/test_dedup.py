import pytest

from ai_ops_hub.dedup import (
    classify,
    content_fingerprint,
    dedup_key,
    normalize_message_id,
)
from ai_ops_hub.parsing import parse_eml, parse_pasted_text
from ai_ops_hub.schemas import DedupDecision

from conftest import read_sample


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<abc@Example.COM>", "abc@example.com"),
        ("  <abc@example.com>  ", "abc@example.com"),
        ("abc@example.com", "abc@example.com"),
        ("<Local.Part@EXAMPLE.com>", "Local.Part@example.com"),  # local part case kept
        ("<no-at-sign>", "no-at-sign"),
        ("", None),
        ("   ", None),
        ("<>", None),
        (None, None),
        ("two words", None),
    ],
)
def test_normalize_message_id(raw, expected):
    assert normalize_message_id(raw) == expected


def test_key_uses_message_id_when_present(settings):
    m = parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings)
    assert dedup_key(m) == "mid:inv-2026-0042@zielonadolina.example"


def test_key_is_deterministic_and_content_based_without_message_id(settings):
    a = parse_eml(read_sample("08_no_message_id_no_date.eml"), settings)
    b = parse_eml(read_sample("08_no_message_id_no_date.eml"), settings)
    assert dedup_key(a) == dedup_key(b)
    assert dedup_key(a).startswith("fp:")


def test_fallback_key_differs_for_different_content():
    a = parse_pasted_text("Subject: s\n\nbody one")
    b = parse_pasted_text("Subject: s\n\nbody two")
    assert dedup_key(a) != dedup_key(b)


def test_fingerprint_ignores_whitespace_and_line_endings():
    a = parse_pasted_text("Subject: s\n\nline one\nline two")
    b = parse_pasted_text("Subject: s\r\n\r\nline  one \r\n line two ")
    assert content_fingerprint(a) == content_fingerprint(b)


def test_fingerprint_ignores_unicode_normalisation_form():
    import unicodedata

    nfc = parse_pasted_text("Subject: s\n\nzażółć")
    decomposed = parse_pasted_text("Subject: s\n\n" + unicodedata.normalize("NFD", "zażółć"))
    assert nfc.body_text != decomposed.body_text  # stored text is NOT rewritten
    assert content_fingerprint(nfc) == content_fingerprint(decomposed)


def test_fingerprint_changes_when_amount_changes(settings):
    a = parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings)
    b = parse_eml(read_sample("02_invoice_dispute_pl_conflict.eml"), settings)
    assert content_fingerprint(a) != content_fingerprint(b)


# --- classify: the explicit conflict rules ---------------------------------


def test_new_message(settings):
    m = parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings)
    r = classify(m, {})
    assert r.decision is DedupDecision.NEW
    assert r.existing_fingerprint is None


def test_identical_redelivery_is_duplicate(settings):
    m = parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings)
    known = {dedup_key(m): content_fingerprint(m)}
    again = parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings)
    r = classify(again, known)
    assert r.decision is DedupDecision.DUPLICATE


def test_same_message_id_different_content_is_conflict_not_discarded(settings):
    original = parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings)
    altered = parse_eml(read_sample("02_invoice_dispute_pl_conflict.eml"), settings)
    assert dedup_key(original) == dedup_key(altered)  # same Message-ID after normalisation
    known = {dedup_key(original): content_fingerprint(original)}
    r = classify(altered, known)
    assert r.decision is DedupDecision.CONFLICT
    assert r.existing_fingerprint == content_fingerprint(original)
    assert r.fingerprint == content_fingerprint(altered)
    assert "human" in r.reason


def test_redelivery_with_different_date_header_is_still_duplicate():
    base = "From: a@b.example\nMessage-ID: <m@x.example>\nDate: {d}\n\nsame body"
    a = parse_pasted_text(base.format(d="Mon, 02 Nov 2026 09:00:00 +0100"))
    b = parse_pasted_text(base.format(d="Mon, 02 Nov 2026 09:00:07 +0100"))
    assert classify(b, {dedup_key(a): content_fingerprint(a)}).decision is DedupDecision.DUPLICATE


def test_no_message_id_cannot_conflict():
    a = parse_pasted_text("Subject: s\n\nbody one")
    b = parse_pasted_text("Subject: s\n\nbody two")
    r = classify(b, {dedup_key(a): content_fingerprint(a)})
    assert r.decision is DedupDecision.NEW  # different content = different key


def test_classify_does_not_mutate_known_map(settings):
    m = parse_eml(read_sample("01_invoice_dispute_pl.eml"), settings)
    known: dict[str, str] = {}
    classify(m, known)
    assert known == {}


def test_eml_and_pasted_copy_of_same_message_are_duplicates(settings):
    eml = parse_eml(read_sample("03_contract_renewal_en.eml"), settings)
    pasted = parse_pasted_text(
        "From: Daniel Reeve <daniel.reeve@northbridge-logistics.example>\n"
        "Subject: Renewal of Master Services Agreement MSA-2025-07\n"
        "Message-ID: <msa-renewal-0003@northbridge-logistics.example>\n\n"
        + eml.body_text
    )
    assert classify(pasted, {dedup_key(eml): content_fingerprint(eml)}).decision is (
        DedupDecision.DUPLICATE
    )
