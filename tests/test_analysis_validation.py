"""Category-specific validation and review reasons. Nothing here approves anything."""

import pytest
from pydantic import ValidationError

from ai_ops_hub.analysis.schemas import AnalysisResult, FieldName, ReviewCode
from ai_ops_hub.schemas import AnalysisMethod

from conftest import run_analysis


def reasons(r):
    return [(x.code, x.field) for x in r.review_reasons]


# --- orders -------------------------------------------------------------------


def test_complete_order_needs_no_review():
    r = run_analysis("Zamawiamy 120 szt. kabli zasilających. Dostawa na 20 listopada 2026.")
    assert r.category.value == "order" and not r.requires_review and r.review_reasons == ()


def test_order_without_product_requires_review():
    r = run_analysis("Prosimy o dostawę 50 szt. do 5 grudnia 2026.")
    assert (ReviewCode.MISSING_REQUIRED_FIELD, FieldName.PRODUCT) in reasons(r)
    assert r.requires_review


def test_order_without_quantity_requires_review():
    r = run_analysis("Zamawiam wkręty M8 ze stali nierdzewnej.")
    assert (ReviewCode.MISSING_REQUIRED_FIELD, FieldName.QUANTITY) in reasons(r)


def test_order_without_product_and_quantity_lists_both_gaps():
    r = run_analysis("We want to order again, same as last time.")
    missing = {f for c, f in reasons(r) if c is ReviewCode.MISSING_REQUIRED_FIELD}
    assert missing == {FieldName.PRODUCT, FieldName.QUANTITY}


def test_missing_amount_is_not_an_error_for_an_order():
    r = run_analysis("We would like to order 40 boxes of A4 paper. Delivery on 18 March 2027.")
    assert r.fields_named(FieldName.AMOUNT) == []
    assert not r.requires_review


def test_missing_amount_is_not_an_error_for_a_complaint():
    r = run_analysis("Reklamacja zamówienia ZAM-1180. Towar uszkodzony.")
    assert r.category.value == "complaint" and r.fields_named(FieldName.AMOUNT) == []
    assert not r.requires_review


# --- other categories -----------------------------------------------------------


def test_complaint_without_reference_requires_review_but_with_reference_does_not():
    assert (ReviewCode.MISSING_REQUIRED_FIELD, FieldName.REFERENCE) in reasons(
        run_analysis("The goods arrived damaged. We want a refund.")
    )
    assert not run_analysis("Order ZAM-1180 arrived damaged. We want a refund.").requires_review


def test_document_request_needs_a_reference():
    assert run_analysis("Please send us a copy of invoice INV-5521.").requires_review is False
    assert run_analysis("Please send us a copy of the invoice.").requires_review is True


def test_inquiry_needs_a_product_or_a_reference():
    r = run_analysis("Do you offer next-day shipping to Poland? How much does it cost?")
    assert r.category.value == "inquiry" and r.requires_review
    assert not run_analysis("Could you tell us the price for 500 pcs of USB-C cables?").requires_review


def test_other_category_always_goes_to_a_human():
    r = run_analysis("Happy holidays to the whole team!")
    assert r.category.value == "other"
    assert [c for c, _ in reasons(r)] == [ReviewCode.NO_CATEGORY_MATCH]


# --- ambiguity -------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, field",
    [
        ("Zamawiamy 5 szt. filtrów. Termin: do 12.12.26.", FieldName.DEADLINE),  # two-digit year
        ("We order 20 pcs of Brake Pads. Please deliver by 03/04/2026.", FieldName.DEADLINE),  # d/m or m/d
        ("We order 40 boxes of A4 paper. Delivery on 18 March.", FieldName.REQUESTED_DATE),  # no year
        ("We order 5 pcs of Plates. Total: $500.00.", FieldName.AMOUNT),  # which dollar?
        ("Zamawiamy 10 szt. lamp. Cena 1500.", FieldName.AMOUNT),  # no currency
        ("Zamawiamy 50-60 szt. opakowań.", FieldName.QUANTITY),  # range
        ("We order approx. 100 pcs of Cable Tie Pack.", FieldName.QUANTITY),  # approximate
        ("Dostawa najpóźniej 31 listopada 2026. Zamawiamy 8 szt. czujników.", FieldName.DEADLINE),  # invalid
    ],
)
def test_ambiguous_or_invalid_material_fields_require_review(body, field):
    r = run_analysis(body)
    assert (ReviewCode.FIELD_NEEDS_REVIEW, field) in reasons(r)
    assert r.requires_review


def test_ambiguous_non_material_field_alone_does_not_force_review():
    r = run_analysis("Zamawiamy 5 szt. filtrów.\nPozdrawiam,\nPiotr")  # first name only
    name = r.fields_named(FieldName.CONTACT_NAME)[0]
    assert name.status.value == "ambiguous"
    assert not r.requires_review


def test_relative_deadline_requires_review():
    r = run_analysis("Please ship 10 pcs of Brake Pads asap.")
    assert any(c is ReviewCode.UNRESOLVED_RELATIVE_DATE for c, _ in reasons(r))


def test_unsupported_values_are_never_invented():
    r = run_analysis(
        "As agreed with Mr. Smith, the price is fixed and payment was already made. "
        "Please confirm the order of 12 units of Widget Alpha."
    )
    assert r.fields_named(FieldName.AMOUNT) == []
    assert r.fields_named(FieldName.CONTACT_NAME) == []
    assert r.fields_named(FieldName.DEADLINE) == [] and r.fields_named(FieldName.REQUESTED_DATE) == []


# --- no approval anywhere ----------------------------------------------------------


def test_result_schema_has_no_approval_or_sending_concept():
    names = set(AnalysisResult.model_fields)
    assert not {n for n in names if "approv" in n or "send" in n or "status" in n}
    assert {c.value for c in ReviewCode}.isdisjoint({"approved", "auto_approved"})


def test_requires_review_must_match_the_reasons():
    base = dict(
        analyzer_name="x", analyzer_version="1", analysis_method=AnalysisMethod.RULE_BASED,
        category="other",
    )
    with pytest.raises(ValidationError):
        AnalysisResult(**base, requires_review=True, review_reasons=())
    ok = run_analysis("Zamawiamy 5 szt. kabli.")
    with pytest.raises(ValidationError):
        AnalysisResult(**{**ok.model_dump(), "requires_review": True})  # reasons empty -> inconsistent


def test_no_result_is_ever_auto_approved_even_when_everything_is_valid():
    r = run_analysis("Zamawiamy 120 szt. kabli zasilających. Dostawa na 20 listopada 2026.")
    assert not r.requires_review  # nothing to flag ...
    assert not hasattr(r, "approved") and "approved" not in r.model_dump_json().lower()  # ... but still no approval
