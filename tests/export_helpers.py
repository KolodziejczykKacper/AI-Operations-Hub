"""Shared helpers for the Stage 6 (Excel export) tests. All data is fictional."""

from __future__ import annotations

from io import BytesIO

from conftest import open_case
from openpyxl import load_workbook
from sqlalchemy import text

from ai_ops_hub.export.workbook import DATA_SHEET, HEADERS

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def complaint(n: int, subject: str | None = None, sender: str = "anna@zielonadolina.example") -> str:
    """A clean complaint (ends up ready_for_review): unique Message-ID, amount, reference, deadline."""
    return (
        f"From: {sender}\nSubject: {subject or f'Sprzeciw wobec faktury FV/2026/{n:04d}'}\n"
        f"Message-ID: <exp-{n}@zielonadolina.example>\n\n"
        f"Dzień dobry,\n\nw nawiązaniu do faktury FV/2026/{n:04d} na kwotę 12 500,00 zł prosimy o\n"
        "korektę. Prosimy o odpowiedź do 20 listopada 2026.\n\nZ poważaniem,\nAnna Wiśniewska\n"
    )


def ambiguous_order(n: int, subject: str = "Order") -> str:
    """Deadline without a year: ambiguous, so the case starts as needs_review."""
    return (
        f"From: jan@zielonadolina.example\nSubject: {subject}\nMessage-ID: <exo-{n}@zielonadolina.example>\n\n"
        "We would like to order 10 pcs of Pump Alpha. Please deliver by 15/12.\n\nBest regards,\nJan Kowalski\n"
    )


def approve(client, case_id: int, version: int = 1, **body):
    r = client.post(f"/cases/{case_id}/approve", json={"expected_version": version, **body})
    assert r.status_code == 200, r.text
    return r.json()["case"]


def new_approved(client, n: int, **mail) -> int:
    case = open_case(client, complaint(n, **mail))
    approve(client, case["id"], case["version"])
    return case["id"]


def export_bytes(client, **params) -> bytes:
    r = client.get("/exports/approved-cases.xlsx", params=params)
    assert r.status_code == 200, r.text
    return r.content


def open_xlsx(content: bytes):
    return load_workbook(BytesIO(content))


def table(content: bytes) -> list[dict]:
    """The data sheet as a list of {header: value} dicts, one per exported case."""
    ws = open_xlsx(content)[DATA_SHEET]
    head = [c.value for c in ws[1]]
    assert tuple(head) == HEADERS
    return [dict(zip(head, [c.value for c in row])) for row in ws.iter_rows(min_row=2)]


def ids(content: bytes) -> list[int]:
    return [r["ID przypadku"] for r in table(content)]


def snapshot_of_db(engine) -> dict:
    """Every row of every table an export could conceivably change."""
    tables = ("messages", "analyses", "audit_events", "cases", "case_corrections", "case_reviews")
    with engine.connect() as conn:
        return {t: [tuple(r) for r in conn.execute(text(f"SELECT * FROM {t} ORDER BY id"))] for t in tables}
