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

## Stage 3: rule-based analysis
`RuleBasedAnalyzer` (deterministic, offline) classifies a message as `order`, `complaint`,
`inquiry`, `document_request` or `other` and extracts reference numbers, products, quantities,
amounts with currency, deadlines / requested dates, names, organizations, e-mails and phones.
Every value keeps its normalized form, its exact source span and a validation status
(`valid` / `ambiguous` / `invalid`). Nothing is guessed: missing years, currencies and identities
are flagged, not filled in. Results are always labelled `analysis_method = rule_based`; scores are
raw rule hits, not probabilities. Nothing is ever approved or sent automatically; conflicting
imports are never analyzed. Instruction-like text inside emails is blanked out and flagged.

Analyses are stored in a separate `analyses` table (one row per message + analyzer version) with an
`analysis_completed` audit event written in the same transaction.

```
.venv\Scripts\python -m ai_ops_hub.cli --db data/local/demo.db import --analyze tests/data/samples/01_invoice_dispute_pl.eml
.venv\Scripts\python -m ai_ops_hub.cli --db data/local/demo.db analyze --pending
.venv\Scripts\python -m ai_ops_hub.analysis.evaluation tests/data/eval/dataset_v1.jsonl --failures
.venv\Scripts\python -m ai_ops_hub.analysis.evaluation tests/data/eval/dataset_challenge_v1.jsonl
```
Three fictional PL/EN datasets live in `tests/data/eval/`:

| file | cases | role |
|---|---|---|
| `dataset_v1.jsonl` | 58 | development / regression set, written together with the rules |
| `dataset_challenge_v1.jsonl` | 20 | messier set; the rules were first measured on it untouched, then fixed for the errors it showed, so it is now also a development set |
| `dataset_holdout_v1.jsonl` | 40 | written BEFORE the quality fixes, never tuned against, pinned by a SHA-256 test |

None of them is an independent benchmark (same author as the rules, tens of cases, no confidence
intervals). Numbers are reproducible with the evaluation command above, never hard-coded.

### Known limitations of the rule-based analyzer
The analyzer matches patterns; it does not understand language. Things it cannot do:
* **Semantics:** questions or requests worded without a known phrase ("Is it possible to pay by
  invoice?", "Can I get a copy of ...") fall to `other` and go to human review; sarcasm, double
  negation and negation outside a three-word window are not handled.
* **Negation** is a simple frame ("not a", "nie jest", "no", "bez"); a negator ends at a comma.
* **Products** are only read after a quantity, a `Product:`/`Item:` label or an order verb;
  model codes ("HP-300") are NOT turned into references, but the product they belong to is missed.
* **Tables, multi-line orders:** quantities and products are listed independently; their pairing
  is not asserted.
* **Names** only after a closing phrase that starts a line, or a contact label; initials are accepted.
* **Dates:** relative expressions, ranges and absence/return dates are flagged, never resolved;
  years and currencies are never inferred ("tys.", spelled-out numbers are not parsed).
* **Prompt injection:** only known phrases are detected; the real defence is that the analyzer
  performs no actions and every result goes to a human.

## Setup and tests
```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```
Tests need no internet and no API keys.
