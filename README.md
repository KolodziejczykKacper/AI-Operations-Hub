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

## Setup and tests
```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```
Tests need no internet and no API keys.
