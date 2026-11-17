import pytest
from pydantic import ValidationError

from ai_ops_hub.schemas import (
    Actor,
    AnalysisMethod,
    AuditEvent,
    AuditEventType,
    ExtractedValue,
    SourceSpan,
    ValueKind,
)

BODY = "Faktura na kwotę 12 500,00 zł płatna do 20 listopada 2026."


def make_value(text: str = "12 500,00 zł") -> ExtractedValue:
    start = BODY.index(text)
    return ExtractedValue(
        kind=ValueKind.AMOUNT,
        normalized="12500.00 PLN",
        span=SourceSpan(start=start, end=start + len(text), text=text),
        method=AnalysisMethod.RULE_BASED,
    )


def test_provenance_keeps_raw_span_and_normalized_value():
    v = make_value()
    assert v.normalized == "12500.00 PLN"
    assert v.span.text == "12 500,00 zł"
    assert v.verify_against(BODY)


def test_provenance_detects_changed_source():
    v = make_value()
    assert not v.verify_against(BODY.replace("12 500", "21 500"))


def test_span_rejects_inconsistent_length():
    with pytest.raises(ValidationError):
        SourceSpan(start=0, end=5, text="abc")


def test_span_rejects_empty_or_reversed_range():
    with pytest.raises(ValidationError):
        SourceSpan(start=5, end=5, text="")
    with pytest.raises(ValidationError):
        SourceSpan(start=5, end=3, text="")


def test_span_rejects_negative_start():
    with pytest.raises(ValidationError):
        SourceSpan(start=-1, end=2, text="ab")


def test_extracted_value_is_immutable():
    v = make_value()
    with pytest.raises(ValidationError):
        v.normalized = "0"


def test_method_labels_distinguish_rule_based_from_llm():
    assert AnalysisMethod.RULE_BASED.value == "rule_based"
    assert AnalysisMethod.RULE_BASED is not AnalysisMethod.LLM


def test_system_cannot_approve_case():
    with pytest.raises(ValidationError):
        AuditEvent(case_id="c1", event_type=AuditEventType.CASE_APPROVED, actor=Actor.SYSTEM)
    with pytest.raises(ValidationError):
        AuditEvent(
            case_id="c1",
            event_type=AuditEventType.CASE_APPROVED,
            actor=Actor.RULE_BASED_ANALYZER,
        )


def test_human_can_approve_case():
    e = AuditEvent(case_id="c1", event_type=AuditEventType.CASE_APPROVED, actor=Actor.HUMAN)
    assert e.timestamp.tzinfo is not None


def test_no_email_sending_event_exists():
    assert not any("send" in t.value or "email" in t.value for t in AuditEventType)


# --- Stage 3: backward-compatible provenance extensions --------------------------

from ai_ops_hub.schemas import SpanSource  # noqa: E402


def test_span_defaults_to_the_body_so_stage1_usage_is_unchanged():
    span = SourceSpan(start=0, end=3, text="abc")
    assert span.source is SpanSource.BODY
    assert span.matches("abcdef") and not span.matches("xyzdef")


def test_subject_spans_are_checked_against_the_subject():
    span = SourceSpan(start=4, end=7, text="PO-", source=SpanSource.SUBJECT)
    assert span.matches("body ignored", subject="Re: PO-7731")
    assert not span.matches("Re: PO-7731")  # no subject supplied: cannot match
    assert not span.matches("Re: PO-7731", subject="Re: XX-7731")


def test_new_value_kinds_exist_and_old_ones_remain():
    assert {"amount", "date", "deadline", "party", "reference", "other"} <= {k.value for k in ValueKind}
    assert {"quantity", "product", "email", "phone"} <= {k.value for k in ValueKind}
