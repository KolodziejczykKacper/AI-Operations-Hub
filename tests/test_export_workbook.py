"""The workbook builder: structure, formatting, cell types and formula safety (no database)."""

from __future__ import annotations

import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

import pytest
from export_helpers import open_xlsx

from ai_ops_hub.export.models import ExportRow, ExportValue
from ai_ops_hub.export.workbook import COLUMNS, DATA_SHEET, HEADERS, INFO_SHEET, build_workbook

NOW = datetime(2026, 11, 17, 9, 30, tzinfo=timezone.utc)
EXPECTED_HEADERS = (
    "ID przypadku", "Kategoria", "Nadawca", "Temat e-maila", "Data importu (UTC)",
    "Data zatwierdzenia (UTC)", "Produkt / usługa", "Ilość", "Jednostka", "Kwota", "Waluta",
    "Termin realizacji", "Żądana data", "Numer referencyjny", "Osoba kontaktowa", "Organizacja",
    "E-mail", "Telefon", "Notatka przy zatwierdzeniu", "Pozostałe notatki", "Zatwierdzono mimo problemów",
    "Uzasadnienie (override)", "Zastąpione problemy", "Pola do weryfikacji (niepotwierdzone)",
    "Poprawki człowieka", "Wersja przypadku",
)


def val(text: str, confirmed: bool = True, status: str = "valid") -> ExportValue:
    return ExportValue(value=text, confirmed=confirmed, status=status)


def make_row(case_id: int = 1, **over) -> ExportRow:
    base = dict(
        case_id=case_id, version=2, category="order", sender="jan@zielonadolina.example",
        subject="Zamówienie pomp", imported_at=NOW, approved_at=NOW,
        fields={
            "product": (val("Pompa Alfa"),), "quantity": (val("10 pcs"),), "amount": (val("12500.00 PLN"),),
            "deadline": (val("2026-12-15"),), "reference": (val("ZAM/2026/001"),),
            "contact_name": (val("Jan Kowalski"),), "email": (val("jan@zielonadolina.example"),),
            "phone": (val("+48600100200"),),
        },
        approval_note=None, other_notes=(), override_used=False, override_reason=None,
        overridden_problems=(), unconfirmed=(), corrections=(),
    )
    base.update(over)
    return ExportRow(**base)


def build(rows, **kw) -> bytes:
    return build_workbook(rows, generated_at=NOW, **kw)


def cell_map(content: bytes, row: int = 2) -> dict:
    ws = open_xlsx(content)[DATA_SHEET]
    return {c.key: ws.cell(row=row, column=i) for i, c in enumerate(COLUMNS, start=1)}


# --- structure ---------------------------------------------------------------------------------


def test_column_order_is_stable_and_documented():
    assert HEADERS == EXPECTED_HEADERS
    assert [c.header for c in COLUMNS] == list(EXPECTED_HEADERS)
    ws = open_xlsx(build([make_row()]))[DATA_SHEET]
    assert tuple(c.value for c in ws[1]) == EXPECTED_HEADERS


def test_two_sheets_data_and_information():
    assert open_xlsx(build([make_row()])).sheetnames == [DATA_SHEET, INFO_SHEET]


def test_header_row_is_frozen_filtered_styled_and_columns_have_widths():
    ws = open_xlsx(build([make_row(1), make_row(2)]))[DATA_SHEET]
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref == "A1:Z3"
    assert ws["A1"].font.bold and ws["A1"].fill.fgColor.rgb.endswith("1F3864")
    assert all((ws.column_dimensions[chr(65 + i)].width or 0) >= 10 for i in range(26))
    assert ws.print_title_rows == "$1:$1"


def test_values_land_in_the_right_columns_with_native_types():
    c = cell_map(build([make_row()]))
    assert c["case_id"].value == 1 and c["case_id"].data_type == "n"
    assert c["category"].value == "Zamówienie"
    assert c["sender"].value == "jan@zielonadolina.example" and c["subject"].value == "Zamówienie pomp"
    assert c["product"].value == "Pompa Alfa" and c["reference"].value == "ZAM/2026/001"
    assert c["quantity"].value == 10 and c["quantity"].data_type == "n" and c["unit"].value == "pcs"
    assert c["amount"].value == 12500 and c["amount"].data_type == "n" and c["amount"].number_format == "#,##0.00"
    assert c["currency"].value == "PLN"
    assert c["deadline"].value == datetime(2026, 12, 15) and c["deadline"].number_format == "yyyy-mm-dd"
    assert c["phone"].value == "+48600100200" and c["phone"].data_type == "s"
    assert c["approved_at"].value == datetime(2026, 11, 17, 9, 30)
    assert c["approved_at"].number_format == "yyyy-mm-dd hh:mm"
    assert c["version"].value == 2


def test_timestamps_are_written_as_naive_utc_whatever_the_input_zone():
    warsaw = timezone(timedelta(hours=1))
    c = cell_map(build([make_row(approved_at=datetime(2026, 11, 17, 10, 30, tzinfo=warsaw))]))
    assert c["approved_at"].value == datetime(2026, 11, 17, 9, 30)


def test_missing_values_are_empty_cells_not_invented_ones():
    c = cell_map(build([make_row(fields={}, sender=None, subject=None, imported_at=None)]))
    for key in ("product", "quantity", "unit", "amount", "currency", "deadline", "requested_date", "reference",
                "contact_name", "organization", "email", "phone", "sender", "subject", "imported_at",
                "approval_note", "other_notes", "override_reason", "overridden_problems", "unconfirmed",
                "corrections"):
        assert c[key].value is None, key


def test_amount_without_a_currency_stays_a_number_and_does_not_invent_one():
    c = cell_map(build([make_row(fields={"amount": (val("500.50"),)})]))
    assert c["amount"].value == 500.5 and c["currency"].value is None


def test_numbers_beyond_excel_precision_are_kept_as_text_not_rounded():
    row = make_row(fields={"amount": (val("1234567890123456 PLN"),), "quantity": (val("1234567890123456"),)})
    c = cell_map(build([row]))
    assert c["amount"].value == "1234567890123456 PLN" and c["amount"].data_type == "s"
    assert c["quantity"].value == "1234567890123456" and c["currency"].value is None


def test_several_values_are_listed_not_silently_reduced_to_one():
    row = make_row(fields={"email": (val("a@x.example"), val("b@x.example")),
                           "amount": (val("10.00 PLN"), val("20.00 PLN"))})
    c = cell_map(build([row]))
    assert c["email"].value == "a@x.example; b@x.example"
    assert c["amount"].value == "10.00 PLN; 20.00 PLN" and c["currency"].value is None


def test_unparseable_date_stays_text_instead_of_becoming_a_wrong_date():
    c = cell_map(build([make_row(fields={"deadline": (val("2026-02-30"),)})]))
    assert c["deadline"].value == "2026-02-30" and c["deadline"].data_type == "s"


def test_unconfirmed_values_are_flagged_text_never_clean_numbers_or_dates():
    row = make_row(
        fields={"deadline": (val("--12-15", False, "ambiguous"),),
                "amount": (val("12500.00 PLN", False, "ambiguous"),),
                "quantity": (val("10", False, "invalid"),)},
        unconfirmed=("Termin realizacji: --12-15 (niejednoznaczne, analiza regułowa)",),
    )
    c = cell_map(build([row]))
    for key, raw in (("deadline", "--12-15"), ("amount", "12500.00 PLN"), ("quantity", "10")):
        assert c[key].value == f"[niepotwierdzone] {raw}" and c[key].data_type == "s"
        assert c[key].fill.fgColor.rgb.endswith("FFF2CC")
    assert c["unconfirmed"].value.startswith("Termin realizacji: --12-15")
    assert c["currency"].value is None and c["unit"].value is None


def test_override_columns_are_filled_only_for_overridden_cases():
    plain = cell_map(build([make_row()]))
    assert plain["override"].value == "NIE" and plain["override_reason"].value is None
    over = cell_map(build([make_row(override_used=True, override_reason="Potwierdzone telefonicznie",
                                    overridden_problems=("field_needs_review: 'deadline' is ambiguous",))]))
    assert over["override"].value == "TAK" and over["override_reason"].value == "Potwierdzone telefonicznie"
    assert over["overridden_problems"].value.startswith("field_needs_review")


def test_notes_and_human_corrections_are_exported_multiline():
    row = make_row(approval_note="Termin potwierdzony.", other_notes=("[notatka] a", "[notatka] b"),
                   corrections=("Termin realizacji: analiza „--12-15” → człowiek „2026-12-15”",))
    c = cell_map(build([row]))
    assert c["approval_note"].value == "Termin potwierdzony."
    assert c["other_notes"].value == "[notatka] a\n[notatka] b"
    assert "człowiek „2026-12-15”" in c["corrections"].value


def test_information_sheet_describes_the_export_honestly():
    content = build([make_row()], skipped_count=2, approved_from=date(2026, 11, 1), approved_to=date(2026, 11, 30))
    ws = open_xlsx(content)[INFO_SHEET]
    text = "\n".join(f"{a.value} {b.value}" for a, b in ws.iter_rows(min_col=1, max_col=2))
    for needle in ("Liczba przypadków w pliku 1", "Pominięte (niespójny zapis zatwierdzenia) 2",
                   "od 2026-11-01 do 2026-11-30", "rule_based", "nie sztuczna inteligencja",
                   "[niepotwierdzone]", "UTC", "2026-11-17 09:30:00"):
        assert needle in text, needle


def test_an_empty_row_list_still_makes_a_valid_workbook():
    ws = open_xlsx(build([]))[DATA_SHEET]
    assert ws.max_row == 1 and ws.auto_filter.ref == "A1:Z1"


def test_same_input_gives_the_same_cells():
    def dump():
        return [[c.value for c in r] for r in open_xlsx(build([make_row(1), make_row(2)]))[DATA_SHEET].iter_rows()]

    assert dump() == dump()


def test_a_few_hundred_rows_build_quickly():
    started = time.monotonic()
    content = build([make_row(i) for i in range(1, 601)])
    assert time.monotonic() - started < 15
    assert open_xlsx(content)[DATA_SHEET].max_row == 601


# --- formula injection -----------------------------------------------------------------------------

EVIL = [
    '=HYPERLINK("http://evil.example/x","click")', "=cmd|' /C calc'!A0", "+SUM(1+1)", "-2+3", "@SUM(A1)",
    "\t=1+1", " =1+1", "​=1+1", "﻿=1+1", "|calc", "=1+1\n=2+2",
]
TEXT_KEYS = ("category", "sender", "subject", "product", "reference", "contact_name", "organization", "email",
             "approval_note", "other_notes", "override_reason", "overridden_problems", "unconfirmed",
             "corrections")


def _evil_row(payload: str) -> ExportRow:
    one = (val(payload),)
    return make_row(
        sender=payload, subject=payload, category=payload,
        fields={"product": one, "reference": one, "contact_name": one, "organization": one, "email": one,
                "phone": one, "quantity": one, "amount": one, "deadline": one, "requested_date": one},
        approval_note=payload, other_notes=(payload,), override_used=True, override_reason=payload,
        overridden_problems=(payload,), unconfirmed=(payload,), corrections=(payload,),
    )


@pytest.mark.parametrize("payload", EVIL)
def test_no_untrusted_value_can_become_a_formula_in_any_cell(payload):
    content = build([_evil_row(payload)])
    for ws in open_xlsx(content).worksheets:
        for row in ws.iter_rows():
            for cell in row:
                assert cell.data_type != "f", (ws.title, cell.coordinate)
                if isinstance(cell.value, str):
                    first = cell.value.lstrip()[:1]
                    # a dangerous first character is only allowed behind the neutralising apostrophe
                    assert first not in "=+-@|" or cell.value.startswith("'"), (ws.title, cell.coordinate)
    c = cell_map(content)
    for key in TEXT_KEYS:
        assert c[key].data_type == "s" and str(c[key].value).startswith("'"), key
    for key in ("phone", "quantity", "amount", "deadline", "requested_date"):
        assert c[key].data_type == "s", key


@pytest.mark.parametrize("payload", EVIL)
def test_the_raw_xml_has_no_formula_hyperlink_or_external_reference(payload):
    content = build([_evil_row(payload)])
    with zipfile.ZipFile(BytesIO(content)) as z:
        names = z.namelist()
        assert not any("externalLink" in n or "vbaProject" in n or n.endswith(".bin") for n in names), names
        for name in names:
            if name.endswith(".xml"):
                xml = z.read(name).decode("utf-8")
                assert "<f>" not in xml and "<f " not in xml, name
                assert "<hyperlink" not in xml, name
            if name.endswith(".rels"):
                assert "External" not in z.read(name).decode("utf-8"), name
    ws = open_xlsx(content)[DATA_SHEET]
    assert not ws._hyperlinks and all(c.hyperlink is None for r in ws.iter_rows() for c in r)


def test_a_formula_inside_a_typed_field_stays_text_with_the_unconfirmed_prefix():
    row = make_row(fields={"amount": (val("=1+1", False, "invalid"),), "deadline": (val("+cmd", False, "invalid"),)})
    c = cell_map(build([row]))
    assert c["amount"].value == "[niepotwierdzone] =1+1" and c["amount"].data_type == "s"
    assert c["deadline"].value == "[niepotwierdzone] +cmd"


def test_confirmed_formula_like_amount_is_text_with_apostrophe_not_a_number_or_formula():
    row = make_row(fields={"amount": (val("=1+1"),), "quantity": (val("-1+1"),)})
    c = cell_map(build([row]))
    assert c["amount"].data_type == "s" and c["amount"].value == "'=1+1"
    assert c["quantity"].data_type == "s" and c["quantity"].value == "'-1+1"


def test_control_characters_never_break_the_file():
    row = make_row(subject="a\x00b\x07c\ud800d", sender="x\x1fy", approval_note="line1\x00\nline2")
    c = cell_map(build([row]))
    assert c["subject"].value == "abcd" and c["sender"].value == "xy" and c["approval_note"].value == "line1\nline2"
