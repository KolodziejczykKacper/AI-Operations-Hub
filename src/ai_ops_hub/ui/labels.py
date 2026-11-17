"""Polish labels for the values the API returns. Unknown values are shown as they are."""

from __future__ import annotations

from datetime import datetime

STATUS_LABELS = {
    "needs_review": "🟠 Do poprawy",
    "ready_for_review": "🔵 Gotowy do przeglądu",
    "approved": "🟢 Zatwierdzony",
    "rejected": "🔴 Odrzucony",
}
# Order used in filters and on the dashboard.
STATUS_ORDER = ("needs_review", "ready_for_review", "approved", "rejected")

CATEGORY_LABELS = {
    "order": "Zamówienie",
    "complaint": "Reklamacja",
    "inquiry": "Zapytanie",
    "document_request": "Prośba o dokument",
    "other": "Inne / nierozpoznane",
}

FIELD_LABELS = {
    "contact_name": "Osoba kontaktowa",
    "organization": "Organizacja",
    "reference": "Numer referencyjny",
    "product": "Produkt",
    "quantity": "Ilość",
    "amount": "Kwota",
    "deadline": "Termin",
    "requested_date": "Żądana data",
    "email": "E-mail",
    "phone": "Telefon",
}
FIELD_HINTS = {
    "amount": "format: 12500.00 PLN",
    "quantity": "format: 50 pcs",
    "deadline": "format: RRRR-MM-DD",
    "requested_date": "format: RRRR-MM-DD",
    "phone": "format: +48600100200",
}

VALIDITY_LABELS = {"valid": "✅ poprawne", "ambiguous": "⚠️ niejednoznaczne", "invalid": "❌ nieprawidłowe"}
ORIGIN_LABELS = {"analyzer": "analizator (rule_based)", "human": "człowiek"}

EVENT_LABELS = {
    "message_imported": "Wiadomość zaimportowana",
    "duplicate_detected": "Wykryto duplikat",
    "conflict_detected": "Wykryto konflikt",
    "analysis_completed": "Analiza wykonana (rule_based)",
    "case_created": "Przypadek utworzony",
    "case_updated": "Poprawki człowieka",
    "status_changed": "Zmiana statusu (automatyczna)",
    "case_note_added": "Notatka dodana",
    "case_approved": "ZATWIERDZONY",
    "case_rejected": "ODRZUCONY",
    "case_reopened": "Otwarty ponownie",
    "analysis_failed": "Analiza Claude nie powiodła się (nic nie zapisano)",
}
ACTION_LABELS = {
    "note": "notatka",
    "approve": "zatwierdzenie",
    "reject": "odrzucenie",
    "reopen": "ponowne otwarcie",
    "set": "ustawiono",
    "revert": "przywrócono wartość analizatora",
}
IMPORT_STATUS_LABELS = {"imported": "zaimportowana", "conflict": "KONFLIKT (nie jest analizowana)"}


def label(mapping: dict[str, str], key: str | None) -> str:
    return mapping.get(key, key) if key else "—"


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "—"
