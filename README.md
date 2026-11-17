# AI Operations Hub

Stage 1 only: message ingestion foundations (no database, API, UI, analyzer or exports yet).

All sample data in `tests/data/samples/` is fictional (reserved `.example` domains).

## Architecture rules (approved)
1. The Streamlit UI will talk to FastAPI over HTTP only (later stages).
2. Same Message-ID + different content = **conflict**, never silently discarded.
3. Source text spans and normalized values are stored together (provenance).
4. Every significant case mutation produces an audit event.
5. Fictional bilingual PL/EN data only.
6. The offline analyzer is always labeled `rule_based`, never "AI".
7. Cases are never approved and emails are never sent automatically.

## Stage 2: persistence and idempotent import
SQLite (SQLModel) with `messages` and `audit_events` tables. `ImportService` stores a
message and its audit events in ONE transaction. Identical re-import = duplicate
(existing record returned); same Message-ID + different content = conflict
(existing record untouched, new content kept as a `conflict` row). Uniqueness is
enforced by database constraints, not only in Python. No API/UI yet.

Manual check (database goes to `data/local/`, ignored by Git):
```
.venv\Scripts\python -m ai_ops_hub.cli --db data/local/demo.db import tests/data/samples/01_invoice_dispute_pl.eml tests/data/samples/02_invoice_dispute_pl_conflict.eml
.venv\Scripts\python -m ai_ops_hub.cli --db data/local/demo.db list
.venv\Scripts\python -m ai_ops_hub.cli --db data/local/demo.db audit 1
```

## Setup and tests
```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```
Tests need no internet and no API keys.
