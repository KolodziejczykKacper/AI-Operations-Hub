"""Offline end-to-end demo over HTTP, fictional data only: import -> rule_based analysis -> review -> Excel export.

Prerequisite: the backend is running on this machine (never calls Claude, sends nothing anywhere):

    .venv\\Scripts\\python -m ai_ops_hub.api --db data/local/demo.db
    .venv\\Scripts\\python scripts/demo_api.py

Use a FRESH database file for a clean run. The script is a demonstration, not a test: it prints what happened and
fails loudly if the exported workbook does not contain exactly the approved cases.
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests" / "data" / "samples"
BASE = os.environ.get("AIOPS_API_URL", "http://127.0.0.1:8000").rstrip("/")


def call(method: str, path: str, body=None, raw: bytes | None = None, ctype: str = "application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": ctype}, method=method)
    try:
        with urllib.request.urlopen(req) as response:  # noqa: S310 (loopback only, checked in main)
            payload = response.read()
            is_json = response.headers.get("content-type", "").startswith("application/json")
            return response.status, (json.loads(payload) if is_json else payload), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), exc.headers


def main() -> int:
    host = urlsplit(BASE).hostname
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"refusing to talk to {host!r}: this demo only runs against a local backend")
        return 2

    print("== 1. import: four .eml files (one is a conflicting variant of another) and two pasted texts")
    ids: dict[str, int] = {}
    for name in ("01_invoice_dispute_pl.eml", "02_invoice_dispute_pl_conflict.eml", "03_contract_renewal_en.eml",
                 "04_delivery_delay_mixed_multipart.eml"):
        status, body, _ = call("POST", "/messages/eml", raw=(SAMPLES / name).read_bytes(), ctype="message/rfc822")
        ids[name] = body["message"]["id"]
        print(f"  {name:40} HTTP {status} decision={body['decision']} message #{body['message']['id']}")
    for name in ("09_payment_reminder_en.txt", "10_reklamacja_pl.txt"):
        status, body, _ = call("POST", "/messages/text", {"text": (SAMPLES / name).read_text(encoding="utf-8")})
        ids[name] = body["message"]["id"]
        print(f"  {name:40} HTTP {status} decision={body['decision']} message #{body['message']['id']}")
    status, body, _ = call("POST", "/messages/eml", raw=(SAMPLES / "01_invoice_dispute_pl.eml").read_bytes(),
                           ctype="message/rfc822")
    print(f"  re-import of the first file              HTTP {status} decision={body['decision']} (nothing was added)")

    print("== 2. rule_based analysis (a conflicting import must be refused)")
    cases: dict[str, int] = {}
    for name, message_id in ids.items():
        status, body, _ = call("POST", f"/messages/{message_id}/analyze", {})
        if status in (200, 201):
            cases[name] = body["case"]["id"]
            print(f"  message #{message_id} -> case #{body['case']['id']} {body['case']['status']}")
        else:
            print(f"  message #{message_id} -> HTTP {status} {body['error']['code']}")

    print("== 3. human review (nothing is approved automatically)")
    approved: list[int] = []
    rejected: list[int] = []
    for case_id in cases.values():
        _, detail, _ = call("GET", f"/cases/{case_id}")
        version, problems = detail["case"]["version"], [p["code"] for p in detail["problems"]]
        if "approve" in detail["allowed_actions"]:
            call("POST", f"/cases/{case_id}/approve", {"expected_version": version, "note": "demo: checked by a human"})
            approved.append(case_id)
            print(f"  case #{case_id}: approved (no problems left)")
        elif not rejected:
            call("POST", f"/cases/{case_id}/reject", {"expected_version": version, "reason": "demo: not enough information"})
            rejected.append(case_id)
            print(f"  case #{case_id}: rejected; problems were {problems}")
        else:
            status, error, _ = call("POST", f"/cases/{case_id}/approve", {"expected_version": version})
            print(f"  case #{case_id}: approve without an override -> HTTP {status} {error['error']['code']}")
            call("POST", f"/cases/{case_id}/approve",
                 {"expected_version": version, "override_reason": "demo: confirmed by phone", "note": "override demo"})
            approved.append(case_id)
            print(f"  case #{case_id}: approved WITH a written override")

    print("== 4. Excel export (read-only; only approved cases)")
    print("  stats:", call("GET", "/cases/stats")[1]["by_status"])
    status, xlsx, headers = call("GET", "/exports/approved-cases.xlsx")
    workbook = load_workbook(io.BytesIO(xlsx))
    sheet = workbook.worksheets[0]
    header = [c.value for c in sheet[1]]
    exported = []
    for row in sheet.iter_rows(min_row=2):
        record = dict(zip(header, [c.value for c in row]))
        exported.append(record["ID przypadku"])
        print(f"  case #{record['ID przypadku']} override={record['Zatwierdzono mimo problemów']} "
              f"method={record['Metoda analizy']} subject={str(record['Temat e-maila'])[:40]!r}")
    if sorted(exported) != sorted(approved):
        print("FAIL: the workbook must contain exactly the approved cases")
        return 1
    if any(c.data_type == "f" for ws in workbook.worksheets for r in ws.iter_rows() for c in r):
        print("FAIL: the workbook contains a formula cell")
        return 1
    out = ROOT / "data" / "local" / "demo-export.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(xlsx)
    print(f"OK: exactly the approved cases {sorted(approved)} were exported ({len(xlsx)} bytes, no formula cells).")
    print(f"    saved to {out} (ignored by Git)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
