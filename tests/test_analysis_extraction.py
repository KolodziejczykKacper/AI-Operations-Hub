"""Field extraction: formats, ambiguity, invalid values, provenance, no guessing."""

import pytest

from ai_ops_hub.analysis.schemas import FieldName
from ai_ops_hub.analysis.schemas import FieldStatus as S
from ai_ops_hub.analysis.schemas import WarningCode as W
from ai_ops_hub.schemas import SpanSource

from conftest import field, run_analysis, values


def codes(f):
    return {w.code for w in f.warnings}


# --------------------------------------------------------------------------- amounts


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Kwota 12 500,00 zł.", "12500.00 PLN"),
        ("Kwota 12 500,00 zl.", "12500.00 PLN"),  # no diacritic
        ("Total EUR 8,400.00.", "8400.00 EUR"),
        ("Total: 3,250.00 USD.", "3250.00 USD"),
        ("Budget EUR 1.250,50.", "1250.50 EUR"),
        ("Cost: 300,00 PLN.", "300.00 PLN"),
        ("Price: £89.99.", "89.99 GBP"),
        ("Cena 4 200 złotych.", "4200.00 PLN"),
        ("Refund of 150 EUR please.", "150.00 EUR"),
    ],
)
def test_amount_formats_normalize_with_explicit_currency(text, expected):
    f = field(run_analysis(text), "amount")
    assert f.value.normalized == expected
    assert f.status is S.VALID and not f.warnings


def test_amount_span_is_the_exact_original_text():
    body = "Cena łączna 12 500,00 zł, płatne z góry."
    f = field(run_analysis(body), "amount")
    assert f.value.span.text == "12 500,00 zł"
    assert body[f.value.span.start : f.value.span.end] == "12 500,00 zł"


def test_dollar_sign_does_not_infer_a_currency():
    f = field(run_analysis("Total: $500.00."), "amount")
    assert f.value.normalized == "500.00"
    assert f.status is S.AMBIGUOUS and W.CURRENCY_AMBIGUOUS in codes(f)


def test_amount_with_cue_but_no_currency_is_ambiguous_not_guessed():
    f = field(run_analysis("Cena 1500."), "amount")
    assert f.value.normalized == "1500.00"
    assert f.status is S.AMBIGUOUS and W.CURRENCY_NOT_STATED in codes(f)


def test_single_separator_before_three_digits_is_flagged():
    f = field(run_analysis("Total: EUR 1,500."), "amount")
    assert f.status is S.AMBIGUOUS and W.NUMBER_FORMAT_AMBIGUOUS in codes(f)
    assert f.value.normalized == "1500.00 EUR"  # read as thousands, but flagged


def test_bare_numbers_without_cue_are_not_amounts():
    r = run_analysis("We have 3500 employees and 2026 was a good year.")
    assert values(r, "amount") == []


def test_percentages_and_quantities_are_not_amounts():
    r = run_analysis("Rabat 15%. Zamawiamy 120 szt.")
    assert values(r, "amount") == []


def test_multiple_amounts_are_all_reported():
    r = run_analysis("Faktura na 9 800 zł zamiast 8 900 zł.")
    assert values(r, "amount") == ["9800.00 PLN", "8900.00 PLN"]


# --------------------------------------------------------------------------- dates


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Prosimy o odpowiedź do 20 listopada 2026.", "2026-11-20"),
        ("Please reply by 20 November 2026.", "2026-11-20"),
        ("Please reply by November 20, 2026.", "2026-11-20"),
        ("Please reply by 3rd of March 2027.", "2027-03-03"),
        ("Termin: do 2026-12-31.", "2026-12-31"),
        ("Termin: do 12.12.2026.", "2026-12-12"),
        ("Deadline: 25/12/2026.", "2026-12-25"),  # 25 cannot be a month: day-first
        ("Deadline: 12/25/2026.", "2026-12-25"),  # 25 cannot be a month: month-first
        ("Deadline: 05/05/2026.", "2026-05-05"),  # both readings identical
        ("Do 30 pazdziernika 2026.", "2026-10-30"),  # no diacritics
        ("Do 30 października 2026.", "2026-10-30"),
        ("Deadline 1 Sept 2026.", "2026-09-01"),
        ("Due before 29 February 2028.", "2028-02-29"),  # leap year
    ],
)
def test_valid_dates_normalize_to_iso(text, expected):
    f = field(run_analysis(text), "deadline")
    assert f.value.normalized == expected and f.status is S.VALID


def test_slash_date_with_both_parts_up_to_12_is_ambiguous_and_lists_candidates():
    f = field(run_analysis("Please deliver by 03/04/2026."), "deadline")
    assert f.status is S.AMBIGUOUS and W.DATE_FORMAT_AMBIGUOUS in codes(f)
    assert f.value.normalized == "03/04/2026"  # original text kept, no guess
    msg = f.warnings[0].message
    assert "2026-04-03" in msg and "2026-03-04" in msg


def test_missing_year_is_not_inferred():
    f = field(run_analysis("Delivery on 18 March, please."), "requested_date")
    assert f.value.normalized == "--03-18"
    assert f.status is S.AMBIGUOUS and W.YEAR_NOT_STATED in codes(f)


def test_leap_day_without_year_is_accepted_but_ambiguous():
    f = field(run_analysis("Do 29 lutego."), "deadline")
    assert f.value.normalized == "--02-29" and f.status is S.AMBIGUOUS


def test_two_digit_year_does_not_guess_the_century():
    f = field(run_analysis("Termin: do 12.12.26."), "deadline")
    assert f.status is S.AMBIGUOUS and W.TWO_DIGIT_YEAR in codes(f)
    assert f.value.normalized == "12.12.26"


@pytest.mark.parametrize(
    "text", ["Dostawa najpóźniej 31 listopada 2026.", "Deadline 2026-02-30.", "Due by 29 February 2027.", "Do 32.01.2026."]
)
def test_impossible_dates_are_invalid_not_corrected(text):
    f = field(run_analysis(text), "deadline" if "Dostawa" not in text else "deadline")
    assert f.status is S.INVALID and W.DATE_INVALID in codes(f)


def test_month_level_deadline_does_not_invent_a_day():
    f = field(run_analysis("Płatność do końca listopada 2026."), "deadline")
    assert f.value.normalized == "2026-11"
    assert f.status is S.AMBIGUOUS and W.DAY_NOT_STATED in codes(f)


def test_date_without_deadline_or_request_cue_is_not_extracted_but_noted():
    body = "The invoice dated 3 November 2026 was paid."
    r = run_analysis(body)
    assert values(r, "deadline") == [] and values(r, "requested_date") == []
    note = [w for w in r.warnings if w.code is W.DATE_WITHOUT_ROLE]
    assert note and note[0].span.text == "3 November 2026"


def test_deadline_and_requested_date_roles():
    r = run_analysis("Dostawa na 20 listopada 2026. Prosimy o potwierdzenie do 10 listopada 2026.")
    assert values(r, "requested_date") == ["2026-11-20"]
    assert values(r, "deadline") == ["2026-11-10"]


def test_abbreviation_dot_does_not_cut_the_sentence_window():
    # "szt." must not be treated as a sentence end, so the delivery cue is still seen
    r = run_analysis("Zamawiam 50 szt. dostawa 3 grudnia 2026.")
    assert values(r, "requested_date") == ["2026-12-03"]


@pytest.mark.parametrize(
    "text",
    ["Odpowiem w ciągu 3 dni roboczych.", "Please ship asap.", "Do końca tygodnia proszę o odpowiedź.",
     "Delivery tomorrow please.", "Prosimy o dostawę w przyszłym tygodniu.", "Within 14 days of receipt."],
)
def test_relative_expressions_are_flagged_not_resolved(text):
    r = run_analysis(text)
    assert any(w.code is W.RELATIVE_DATE_NOT_RESOLVED for w in r.warnings)
    assert values(r, "deadline") == [] and values(r, "requested_date") == []


def test_today_without_a_deadline_cue_is_not_flagged():
    r = run_analysis("Thank you for your call today.")
    assert not any(w.code is W.RELATIVE_DATE_NOT_RESOLVED for w in r.warnings)


# --------------------------------------------------------------------------- quantities


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Zamawiamy 120 szt. kabli.", "120 pcs"),
        ("Zamawiamy 30 sztuk zaworów.", "30 pcs"),
        ("We order 200 pcs of Widget.", "200 pcs"),
        ("Need 15 licenses of Suite.", "15 licenses"),
        ("10 palet cementu", "10 pallets"),
        ("12 units of Widget", "12 units"),
        ("2 kg of nails", "2 kg"),
        ("1 000 szt. kabli", "1000 pcs"),
        ("Quantity: 40", "40"),
        ("Ilość: 7", "7"),
    ],
)
def test_quantity_normalization(text, expected):
    f = field(run_analysis(text), "quantity")
    assert f.value.normalized == expected and f.status is S.VALID


def test_times_form_gives_unitless_quantity():
    assert values(run_analysis("We need 3 x Laser Printer Pro."), "quantity") == ["3"]


def test_xerox_is_not_a_times_sign():
    assert values(run_analysis("We need 10 xerox copies."), "quantity") == []


def test_range_and_approximate_quantities_are_ambiguous():
    f = field(run_analysis("Zamawiamy 50-60 szt. opakowań."), "quantity")
    assert f.value.normalized == "50-60 pcs" and W.QUANTITY_RANGE in codes(f)
    g = field(run_analysis("We want approx. 100 pcs of Cable."), "quantity")
    assert g.status is S.AMBIGUOUS and W.APPROXIMATE_QUANTITY in codes(g)
    h = field(run_analysis("Zamawiamy ok. 80 szt. rur."), "quantity")
    assert h.status is S.AMBIGUOUS


def test_durations_in_days_are_not_quantities():
    assert values(run_analysis("Odpowiedź w terminie 14 dni."), "quantity") == []


def test_several_quantities_are_kept_and_flagged_without_guessing_pairs():
    r = run_analysis("Zamawiamy 10 szt. śrub M6 oraz 20 szt. nakrętek M6.")
    assert values(r, "quantity") == ["10 pcs", "20 pcs"]
    assert any(w.code is W.MULTIPLE_QUANTITIES for w in r.warnings)


# --------------------------------------------------------------------------- products


def test_product_after_quantity_and_stops_at_sentence_end():
    r = run_analysis("Zamawiamy 120 szt. kabli zasilających. Prosimy o szybką realizację.")
    assert values(r, "product") == ["kabli zasilających"]


def test_new_sentence_after_abbreviation_dot_is_not_a_product():
    r = run_analysis("Zamawiamy 120 szt. Prosimy o szybką realizację.")
    assert values(r, "product") == []


def test_product_stops_at_a_stop_word_and_keeps_original_case():
    r = run_analysis("We would like to order 200 pcs of Industrial Sensor X2 by 30 November 2026.")
    assert values(r, "product") == ["Industrial Sensor X2"]


def test_product_label():
    assert values(run_analysis("Hello.\nItem: X-200 Controller\nThanks"), "product") == ["X-200 Controller"]
    assert values(run_analysis("Produkt: Pompa wodna P1."), "product") == ["Pompa wodna P1"]


def test_order_verb_with_unitless_number():
    r = run_analysis("Zamawiamy 3 komputery Dell Latitude za łączną kwotę 14 700 zł.")
    assert values(r, "product") == ["komputery Dell Latitude"]
    assert values(r, "quantity") == ["3"]


def test_defect_adjectives_pronouns_and_months_are_not_products():
    assert values(run_analysis("Reklamacja: 120 szt. wadliwych."), "product") == []
    assert values(run_analysis("Please return 5 pcs of them."), "product") == []
    assert values(run_analysis("Prosimy o dostawę 15 listopada 2026."), "product") == []


def test_approximation_word_is_not_a_product():
    r = run_analysis("We would like to order approx. 100 pcs of Cable Tie Pack.")
    assert values(r, "product") == ["Cable Tie Pack"]


def test_articles_are_stripped_from_products():
    r = run_analysis("Please ship 4 pcs of the Hydraulic Pump.")
    assert values(r, "product") == ["Hydraulic Pump"]


def test_overlong_phrases_are_dropped_instead_of_truncated():
    # eight words, no stop words: too long to be a product name, so nothing is guessed
    r = run_analysis("Zamawiamy 5 szt. aaa bbb ccc ddd eee fff ggg hhh.")
    assert values(r, "product") == []
    # more than ~70 characters without a terminator: also dropped
    r = run_analysis("Zamawiamy 5 szt. " + "x" * 90 + ".")
    assert values(r, "product") == []


# --------------------------------------------------------------------------- references


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Dotyczy faktury FV/2026/0042.", ["FV/2026/0042"]),
        ("Order PO-7731 is late.", ["PO-7731"]),
        ("Umowa MSA-2025-07 wygasa.", ["MSA-2025-07"]),
        ("Numer przesyłki: nr 77881234.", ["77881234"]),
        ("See order no. 2026-0099.", ["2026-0099"]),
        ("Zamówienie nr 4400123 czeka.", ["4400123"]),
        ("Refund for order #55021.", ["55021"]),
        ("Ticket ZX9981 open.", ["ZX9981"]),
    ],
)
def test_reference_numbers(text, expected):
    assert values(run_analysis(text), "reference") == expected


def test_bare_digits_need_an_explicit_number_marker():
    assert values(run_analysis("Faktura 12500 zł do zapłaty."), "reference") == []
    assert values(run_analysis("Order 12 pcs."), "reference") == []


def test_dates_and_emails_are_not_references():
    assert values(run_analysis("Faktura z dnia 15.10.2026."), "reference") == []
    r = run_analysis("Write to PO-7731@northbridge-logistics.example please.")
    assert values(r, "reference") == [] and values(r, "email") == ["po-7731@northbridge-logistics.example"]


def test_reference_in_subject_and_body_is_reported_once_with_body_span():
    r = run_analysis("Please check invoice INV-5521 today.", subject="Invoice INV-5521")
    f = field(r, "reference")
    assert f.value.span.source is SpanSource.BODY


def test_reference_found_only_in_subject_has_a_subject_span():
    r = run_analysis("Please advise.", subject="Re: PO-7731")
    f = field(r, "reference")
    assert f.value.span.source is SpanSource.SUBJECT
    assert r.verify_provenance("Please advise.", "Re: PO-7731")
    assert not r.verify_provenance("Please advise.", "Re: PO-0000")  # subject text matters


# --------------------------------------------------------------------------- names and organizations


def test_name_after_closing_phrase_and_organization():
    r = run_analysis("Hello.\nBest regards,\nDaniel Reeve\nNorthbridge Logistics Ltd")
    assert values(r, "contact_name") == ["Daniel Reeve"]
    assert values(r, "organization") == ["Northbridge Logistics Ltd"]


def test_name_on_the_same_line_as_the_closing():
    assert values(run_analysis("Thanks.\nKind regards, Helen Marsh"), "contact_name") == ["Helen Marsh"]


def test_polish_name_with_diacritics():
    r = run_analysis("Dzień dobry.\nZ poważaniem,\nŁukasz Żółć")
    assert values(r, "contact_name") == ["Łukasz Żółć"]


def test_name_after_contact_label():
    r = run_analysis("Kontakt: Piotr Zając, piotr.zajac@zielonadolina.example")
    assert values(r, "contact_name") == ["Piotr Zając"]
    assert values(run_analysis("Please contact Anna Nowak at the office."), "contact_name") == ["Anna Nowak"]


def test_single_name_token_is_partial_and_flagged():
    f = field(run_analysis("Cześć.\nThanks,\nSam"), "contact_name")
    assert f.status is S.AMBIGUOUS and W.PARTIAL_NAME in codes(f)


@pytest.mark.parametrize(
    "tail", ["Customer Service", "Sales Team", "Zespół Obsługi", "Northbridge Logistics Ltd", "Dział Sprzedaży"]
)
def test_teams_and_companies_are_not_person_names(tail):
    assert values(run_analysis(f"Hello.\nRegards,\n{tail}"), "contact_name") == []


def test_a_name_in_running_text_is_not_extracted():
    # no closing phrase, no label: identity is not inferred
    r = run_analysis("As agreed with Mr. Smith and Jan Kowalski the price is fixed.")
    assert values(r, "contact_name") == []


def test_multiple_different_names_are_flagged_without_picking_one():
    r = run_analysis("Kontakt: Anna Nowak.\nRegards,\nPeter Black")
    assert sorted(values(r, "contact_name")) == ["Anna Nowak", "Peter Black"]
    assert any(w.code is W.MULTIPLE_NAMES for w in r.warnings)


def test_organization_legal_forms_and_whitespace_normalization():
    r = run_analysis("Faktura dla Zielona   Dolina Sp. z o.o. oraz Fabryka Przykładów S.A.")
    assert values(r, "organization") == ["Zielona Dolina Sp. z o.o.", "Fabryka Przykładów S.A."]


# --------------------------------------------------------------------------- contact info


def test_email_is_lowercased_and_trailing_dot_removed():
    r = run_analysis("Write to Anna.Nowak@ZielonaDolina.example.")
    f = field(r, "email")
    assert f.value.normalized == "anna.nowak@zielonadolina.example"
    assert f.value.span.text == "Anna.Nowak@ZielonaDolina.example"


def test_email_with_consecutive_dots_is_invalid():
    f = field(run_analysis("Mail: a..b@x.example"), "email")
    assert f.status is S.INVALID


@pytest.mark.parametrize(
    "text, expected, warn",
    [
        ("tel. +48 600 100 200", "+48600100200", None),
        ("Phone: +44 20 7946 0000", "+442079460000", None),
        ("Please call me: +48 22 123 45 67", "+48221234567", None),
        ("kom. 0048 600 700 800", "+48600700800", None),
        ("Visit us. Reach +1 (555) 010-9999 anytime", "+15550109999", None),
        ("tel. 600 100 200", "600100200", W.NO_COUNTRY_CODE),
    ],
)
def test_phone_normalization(text, expected, warn):
    f = field(run_analysis(text), "phone")
    assert f.value.normalized == expected and f.status is S.VALID
    assert (warn in codes(f)) if warn else not f.warnings


def test_phone_without_cue_or_plus_is_not_extracted():
    assert values(run_analysis("Order 600100200 shipped, invoice 123456789."), "phone") == []


def test_phone_with_wrong_digit_count_is_invalid():
    r = run_analysis("tel. 123 4567 89012 3456 7")  # 17 digits
    phones = r.fields_named(FieldName.PHONE)
    assert phones and all(p.status is S.INVALID for p in phones)
    assert W.PHONE_LENGTH_INVALID in codes(phones[0])


def test_dates_are_not_phone_numbers():
    assert values(run_analysis("tel. 15.10.2026"), "phone") == []


# --------------------------------------------------------------------------- provenance in general


def test_every_extracted_value_has_an_exact_span_and_method_rule_based():
    body = (
        "Zamawiamy 120 szt. kabli. Dostawa na 20 listopada 2026. Cena 12 500,00 zł.\n"
        "Z poważaniem,\nAnna Nowak\nZielona Dolina Sp. z o.o.\ntel. +48 600 100 200 anna@zielonadolina.example"
    )
    r = run_analysis(body, subject="Zamówienie ZAM-2201")
    assert r.fields
    for f in r.fields:
        src = body if f.value.span.source is SpanSource.BODY else "Zamówienie ZAM-2201"
        assert src[f.value.span.start : f.value.span.end] == f.value.span.text
        assert f.value.method.value == "rule_based"
        assert f.value.normalized  # never empty
    assert r.verify_provenance(body, "Zamówienie ZAM-2201")


def test_fields_do_not_overlap_each_other():
    r = run_analysis("Zamawiamy 120 szt. kabli. Cena 12 500,00 zł. Dostawa na 20 listopada 2026.")
    spans = sorted((f.value.span.start, f.value.span.end, f.value.span.source.value) for f in r.fields)
    for (s1, e1, k1), (s2, e2, k2) in zip(spans, spans[1:]):
        assert k1 != k2 or e1 <= s2


def test_nothing_is_invented_from_an_empty_or_unrelated_text():
    for body in ["", "   ", "Hello!", "Dziękujemy.", "..."]:
        r = run_analysis(body)
        assert r.fields == ()
        assert r.category.value == "other"
