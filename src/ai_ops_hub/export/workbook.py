"""Build the .xlsx workbook from export rows (pure: no database, no clock, no network).

Rules the workbook follows
--------------------------
* Nothing is invented: a value the case does not state is an EMPTY cell, never "N/A" or 0.
* A value the analyzer was unsure about and no human confirmed is written as TEXT with the
  prefix ``[niepotwierdzone]`` on a yellow background, never as a clean number or date.
* Every piece of untrusted text goes through ``safe_text`` and is stored with an explicit
  string type, so it can never become a formula; no hyperlinks are ever created.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from ai_ops_hub.export.models import ExportRow, ExportValue
from ai_ops_hub.export.safe import clean, safe_text

DATA_SHEET = "Zatwierdzone przypadki"
INFO_SHEET = "Informacje"
UNCONFIRMED_PREFIX = "[niepotwierdzone] "
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

CATEGORY_PL = {
    "order": "Zamówienie",
    "complaint": "Reklamacja",
    "inquiry": "Zapytanie",
    "document_request": "Prośba o dokument",
    "other": "Inne / nierozpoznane",
}
FIELD_PL = {
    "contact_name": "Osoba kontaktowa",
    "organization": "Organizacja",
    "reference": "Numer referencyjny",
    "product": "Produkt / usługa",
    "quantity": "Ilość",
    "amount": "Kwota",
    "deadline": "Termin realizacji",
    "requested_date": "Żądana data",
    "email": "E-mail",
    "phone": "Telefon",
}
STATUS_PL = {"valid": "poprawne", "ambiguous": "niejednoznaczne", "invalid": "nieprawidłowe"}

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AMOUNT = re.compile(r"^(\d{1,15}(?:\.\d{1,4})?)(?: ([A-Z]{3}))?$")
_QUANTITY = re.compile(r"^(\d{1,15}(?:\.\d{1,4})?)(?: (\S.{0,40}))?$")


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    width: int
    wrap: bool = False


# The order of this tuple IS the order of the columns in the file (a stable contract).
COLUMNS: tuple[Column, ...] = (
    Column("case_id", "ID przypadku", 12),
    Column("category", "Kategoria", 20),
    Column("sender", "Nadawca", 32),
    Column("subject", "Temat e-maila", 40, wrap=True),
    Column("imported_at", "Data importu (UTC)", 19),
    Column("approved_at", "Data zatwierdzenia (UTC)", 19),
    Column("product", "Produkt / usługa", 28, wrap=True),
    Column("quantity", "Ilość", 12),
    Column("unit", "Jednostka", 12),
    Column("amount", "Kwota", 16),
    Column("currency", "Waluta", 10),
    Column("deadline", "Termin realizacji", 18),
    Column("requested_date", "Żądana data", 18),
    Column("reference", "Numer referencyjny", 22, wrap=True),
    Column("contact_name", "Osoba kontaktowa", 24, wrap=True),
    Column("organization", "Organizacja", 28, wrap=True),
    Column("email", "E-mail", 30),
    Column("phone", "Telefon", 18),
    Column("approval_note", "Notatka przy zatwierdzeniu", 36, wrap=True),
    Column("other_notes", "Pozostałe notatki", 40, wrap=True),
    Column("override", "Zatwierdzono mimo problemów", 16),
    Column("override_reason", "Uzasadnienie (override)", 40, wrap=True),
    Column("overridden_problems", "Zastąpione problemy", 50, wrap=True),
    Column("unconfirmed", "Pola do weryfikacji (niepotwierdzone)", 44, wrap=True),
    Column("corrections", "Poprawki człowieka", 50, wrap=True),
    Column("version", "Wersja przypadku", 12),
    Column("analysis_method", "Metoda analizy", 30),
)
HEADERS: tuple[str, ...] = tuple(c.header for c in COLUMNS)

_HEADER_FILL = PatternFill("solid", fgColor="1F3864")
_UNCONFIRMED_FILL = PatternFill("solid", fgColor="FFF2CC")
_OVERRIDE_FILL = PatternFill("solid", fgColor="FCE4D6")
_THIN = Side(style="thin", color="BFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_DATETIME_FORMAT = "yyyy-mm-dd hh:mm"
_DATE_FORMAT = "yyyy-mm-dd"
_MONEY_FORMAT = "#,##0.00"


# --- cell writers ---------------------------------------------------------------------------


def put_text(cell: Cell, value: object, *, multiline: bool = False) -> None:
    """Store untrusted text as a STRING cell, whatever it starts with."""
    cell.value = safe_text(value, multiline=multiline)
    cell.data_type = "s"  # explicit: openpyxl would otherwise turn a leading '=' into a formula


def _excel_number(raw: str) -> float | None:
    """A float only when Excel can hold the number exactly enough (<= 15 significant digits)."""
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    digits = len(number.as_tuple().digits)
    return float(number) if digits <= 15 else None


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)


def _display(values: tuple[ExportValue, ...]) -> str:
    parts = [(v.value if v.confirmed else UNCONFIRMED_PREFIX + v.value) for v in values]
    return "; ".join(parts)


def _single_confirmed(values: tuple[ExportValue, ...]) -> str | None:
    return values[0].value if len(values) == 1 and values[0].confirmed else None


def _write_value_cells(
    ws: Worksheet, r: int, columns: dict[str, int], name: str, values: tuple[ExportValue, ...]
) -> None:
    """Write one field into its column(s): typed when it is one confirmed, well-formed value;
    otherwise as honest text. Absent values leave the cell empty."""
    if not values:
        return
    col = columns[name]
    single = _single_confirmed(values)
    flagged = any(not v.confirmed for v in values)

    def as_text(target: str = name) -> None:
        cell = ws.cell(row=r, column=columns[target])
        put_text(cell, _display(values), multiline=False)
        if flagged:
            cell.fill = _UNCONFIRMED_FILL

    if name in ("deadline", "requested_date") and single and _DATE.match(single):
        try:
            cell = ws.cell(row=r, column=col, value=date.fromisoformat(single))
            cell.number_format = _DATE_FORMAT
            cell.alignment = Alignment(horizontal="left", vertical="top")
            return
        except ValueError:
            pass
    elif name == "amount" and single and (m := _AMOUNT.match(single)):
        number = _excel_number(m.group(1))
        if number is not None:
            cell = ws.cell(row=r, column=col, value=number)
            cell.number_format = _MONEY_FORMAT
            if m.group(2):
                put_text(ws.cell(row=r, column=columns["currency"]), m.group(2))
            return
    elif name == "quantity" and single and (m := _QUANTITY.match(single)):
        number = _excel_number(m.group(1))
        if number is not None:
            ws.cell(row=r, column=col, value=number)
            if m.group(2):
                put_text(ws.cell(row=r, column=columns["unit"]), m.group(2))
            return
    as_text()


def _row_cells(ws: Worksheet, r: int, row: ExportRow, columns: dict[str, int]) -> None:
    def text(key: str, value: object, multiline: bool = False) -> Cell:
        cell = ws.cell(row=r, column=columns[key])
        put_text(cell, value, multiline=multiline)
        return cell

    ws.cell(row=r, column=columns["case_id"], value=int(row.case_id))
    text("category", CATEGORY_PL.get(row.category, row.category))
    if row.sender:
        text("sender", row.sender)
    if row.subject:
        text("subject", row.subject)
    for key, when in (("imported_at", row.imported_at), ("approved_at", row.approved_at)):
        naive = _naive_utc(when)
        if naive is not None:
            ws.cell(row=r, column=columns[key], value=naive).number_format = _DATETIME_FORMAT

    for name in FIELD_PL:
        _write_value_cells(ws, r, columns, name, row.fields.get(name, ()))

    if row.approval_note:
        text("approval_note", row.approval_note, multiline=True)
    if row.other_notes:
        text("other_notes", "\n".join(row.other_notes), multiline=True)
    override = text("override", "TAK" if row.override_used else "NIE")
    if row.override_used:
        override.fill = _OVERRIDE_FILL
        if row.override_reason:
            text("override_reason", row.override_reason, multiline=True).fill = _OVERRIDE_FILL
        if row.overridden_problems:
            text("overridden_problems", "\n".join(row.overridden_problems), multiline=True).fill = _OVERRIDE_FILL
    if row.unconfirmed:
        text("unconfirmed", "\n".join(row.unconfirmed), multiline=True).fill = _UNCONFIRMED_FILL
    if row.corrections:
        text("corrections", "\n".join(row.corrections), multiline=True)
    ws.cell(row=r, column=columns["version"], value=int(row.version))
    method = "llm: Claude API" if row.analysis_method == "llm" else row.analysis_method
    text("analysis_method", f"{method} {row.analyzer_version}".strip())


def _style_data_sheet(ws: Worksheet, last_row: int) -> None:
    wrap = {c.key for c in COLUMNS if c.wrap}
    for idx, col in enumerate(COLUMNS, start=1):
        letter = get_column_letter(idx)
        ws.column_dimensions[letter].width = col.width
        head = ws.cell(row=1, column=idx)
        head.font = Font(bold=True, color="FFFFFF")
        head.fill = _HEADER_FILL
        head.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        head.border = _BORDER
        for r in range(2, last_row + 1):
            cell = ws.cell(row=r, column=idx)
            cell.border = _BORDER
            horizontal = cell.alignment.horizontal
            cell.alignment = Alignment(vertical="top", wrap_text=col.key in wrap, horizontal=horizontal)
    ws.row_dimensions[1].height = 34
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(last_row, 1)}"
    ws.sheet_view.zoomScale = 100
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"


def _info_sheet(
    wb: Workbook,
    *,
    generated_at: datetime,
    case_count: int,
    skipped_count: int,
    approved_from: date | None,
    approved_to: date | None,
) -> None:
    ws = wb.create_sheet(INFO_SHEET)
    window = (
        "wszystkie daty zatwierdzenia"
        if approved_from is None and approved_to is None
        else f"od {approved_from or '...'} do {approved_to or '...'} (UTC, włącznie)"
    )
    lines: list[tuple[str, str]] = [
        ("Eksport", "AI Operations Hub: zatwierdzone przypadki"),
        ("Wygenerowano (UTC)", generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        ("Liczba przypadków w pliku", str(case_count)),
        ("Zakres dat zatwierdzenia", window),
        ("Pominięte (niespójny zapis zatwierdzenia)", str(skipped_count)),
        ("", ""),
        ("Co zawiera plik", "Wyłącznie przypadki o bieżącym statusie 'zatwierdzony'. Odrzucone, otwarte, "
         "ponownie otwarte (do czasu nowego zatwierdzenia) i konfliktowe wiadomości są wykluczone."),
        ("Skąd są wartości", "Z danych zatwierdzonych przez człowieka (migawka decyzji): poprawki człowieka "
         "mają pierwszeństwo przed wynikiem analizy. Nic nie jest uzupełniane: brak wartości = pusta komórka."),
        ("[niepotwierdzone]", "Wartość z analizy automatycznej (reguły lub model), która była niejednoznaczna lub nieprawidłowa i której "
         "człowiek nie potwierdził. Zapisana jako tekst na żółtym tle. Nie traktuj jej jako ustalonego faktu."),
        ("Zatwierdzono mimo problemów", "TAK oznacza zatwierdzenie z pisemnym uzasadnieniem mimo wykrytych "
         "problemów; uzasadnienie i lista problemów są w osobnych kolumnach."),
        ("Analiza", "Kolumna 'Metoda analizy' mówi, skąd pochodzi analiza przypadku: rule_based (dopasowanie wzorców, "
         "nie sztuczna inteligencja) albo llm (model Claude, tylko gdy użytkownik jawnie wybrał ją dla przypadku). "
         "W obu przypadkach wartości zatwierdził człowiek."),
        ("Bezpieczeństwo", "Teksty z e-maili są zapisane jako tekst. Wartość zaczynająca się od = + - @ | "
         "dostaje na początku apostrof, aby nie została uruchomiona jako formuła."),
        ("Daty", "Daty i godziny są w UTC."),
        ("Źródło", "Plik tylko do odczytu informacyjnego: eksport nie zmienia statusów przypadków."),
    ]
    for i, (key, value) in enumerate(lines, start=1):
        k = ws.cell(row=i, column=1)
        v = ws.cell(row=i, column=2)
        put_text(k, key)
        put_text(v, value, multiline=True)
        k.font = Font(bold=True)
        k.alignment = Alignment(vertical="top")
        v.alignment = Alignment(vertical="top", wrap_text=True)
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 110


def build_workbook(
    rows: list[ExportRow],
    *,
    generated_at: datetime,
    skipped_count: int = 0,
    approved_from: date | None = None,
    approved_to: date | None = None,
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = DATA_SHEET
    columns = {c.key: i for i, c in enumerate(COLUMNS, start=1)}
    for key, idx in columns.items():
        put_text(ws.cell(row=1, column=idx), next(c.header for c in COLUMNS if c.key == key))
    for r, row in enumerate(rows, start=2):
        _row_cells(ws, r, row, columns)
    _style_data_sheet(ws, len(rows) + 1)
    _info_sheet(
        wb,
        generated_at=generated_at,
        case_count=len(rows),
        skipped_count=skipped_count,
        approved_from=approved_from,
        approved_to=approved_to,
    )
    wb.properties.creator = "AI Operations Hub"
    wb.properties.title = "Zatwierdzone przypadki"
    wb.properties.created = _naive_utc(generated_at)
    wb.properties.modified = _naive_utc(generated_at)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


__all__ = ["COLUMNS", "HEADERS", "DATA_SHEET", "INFO_SHEET", "XLSX_MIME", "build_workbook", "clean"]
