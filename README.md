# AI Operations Hub

Local demo of an email-handling workflow: import business emails, analyze them with a
**rule-based** analyzer, correct the extracted data and explicitly approve or reject cases through a
REST API (Stage 4) or a Streamlit dashboard (Stage 5). No AI model, no e-mail sending, no
external actions.

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

## Stage 4: REST API, cases and human review

```
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m ai_ops_hub.api --db data/local/demo.db
```
Swagger UI: **http://127.0.0.1:8000/docs** (OpenAPI: `/openapi.json`). The server listens on
`127.0.0.1` only; any other `--host` is refused unless `--allow-remote` is given (see limitations).
A step-by-step walkthrough in Polish is in [docs/DEMO_PL.md](docs/DEMO_PL.md).

### Rule-based analysis is not AI
The analyzer matches patterns (a lexicon, regular expressions, date/number parsing). It understands
no language, learns nothing and calls no model. Every result is labelled `analysis_method=rule_based`;
its scores are rule hits, not probabilities. A future LLM adapter would plug in behind the same
`Analyzer` interface and would be labelled differently. **A human decides every case.**

### Three separate states
| what | where | values |
|---|---|---|
| import state | `messages.import_status` | `imported`, `conflict` |
| what the analyzer said | `analyses` (immutable, one row per analyzer version) | category, fields + source spans, warnings |
| review state | `cases.status` | `needs_review`, `ready_for_review`, `approved`, `rejected` |

A **case** (one per message) is created when a message is analyzed. A message imported as a
`conflict` is never analyzed and never gets a case (service check + database trigger). A case keeps
reviewing the analysis it was created from; a later analyzer version adds a new analysis row but does
not change the case.

### Review status transitions
| from | to | how |
|---|---|---|
| needs_review <-> ready_for_review | | automatic, recomputed from the **effective** data after each edit (audited as `system`); never a decision |
| needs_review / ready_for_review | approved | `POST /cases/{id}/approve`; blocked (422) while problems remain, unless `override_reason` is given |
| needs_review / ready_for_review | rejected | `POST /cases/{id}/reject` (reason required) |
| approved / rejected | needs_review / ready_for_review | `POST /cases/{id}/reopen` (reason required); the status is recomputed from the data |

Everything else is illegal (409): approved to rejected directly, editing a closed case, deciding twice.
`ready_for_review` only means "no known problems"; it is **not** an approval. A trigger in the
database refuses `approved`/`rejected` unless a matching human decision record exists, and a case
can never be created already decided.

### Endpoints
| method and path | purpose | success | errors |
|---|---|---|---|
| `GET /health` | liveness + database check | 200 | |
| `POST /messages/eml` | import a `.eml` (raw bytes; `Content-Type: message/rfc822` or `application/octet-stream`) | 201 new, 200 duplicate, **409 conflict** (stored; body = import result) | 413 too large, 415 content type, 422 empty/malformed |
| `POST /messages/text` | import pasted text `{"text": "..."}` | 201 / 200 / 409 as above | 400 invalid JSON, 413, 422 |
| `GET /messages?limit&offset` | list messages (limit 1-200) | 200 | 422 |
| `GET /messages/{id}` | message with original body and case link | 200 | 404 |
| `POST /messages/{id}/analyze` | run the analyzer, open the case (idempotent) | 201 new, 200 existing | 404, **409 conflicting import** |
| `GET /messages/{id}/analysis` | latest analysis with warnings | 200 | 404 |
| `GET /messages/{id}/analyses` | all analyses, oldest first | 200 | 404 |
| `GET /messages/{id}/audit` | audit trail of the message | 200 | 404 |
| `GET /cases?status&limit&offset` | list cases | 200 | 422 |
| `GET /cases/{id}` | original email, effective fields + source spans, problems, allowed actions | 200 | 404 |
| `PATCH /cases/{id}` | correct values and/or add a note | 200 | 404, 409, 422 |
| `POST /cases/{id}/approve` | explicit approval | 200 | 404, 409, 422 `approval_blocked` |
| `POST /cases/{id}/reject` | explicit rejection | 200 | 404, 409 |
| `POST /cases/{id}/reopen` | reopen a decided case | 200 | 404, 409 |
| `GET /cases/{id}/history` | all corrections and decisions | 200 | 404 |
| `GET /cases/{id}/audit` | audit trail (import, analysis, case events) | 200 | 404 |

All errors share one shape: `{"error": {"code": "...", "message": "...", "details": {...}}}`.
Codes: `validation_error`, `invalid_json`, `payload_too_large`, `input_too_large`, `empty_input`,
`malformed_message`, `unsupported_media_type`, `message_not_found`, `case_not_found`,
`conflict_not_analyzable`, `case_not_available`, `invalid_transition`, `case_not_editable`,
`version_conflict`, `approval_blocked`, `invalid_correction`, `not_found`, `method_not_allowed`,
`internal_error`. The 409 answer to a conflicting *import* is the exception: it carries the normal
import result because the conflicting message is stored.

### Example requests (fictional data, Windows `cmd`)
```
curl -X POST http://127.0.0.1:8000/messages/text -H "Content-Type: application/json" -d "{\"text\": \"From: jan@zielonadolina.example\nSubject: Order\n\nWe would like to order 10 pcs of Pump Alpha. Please deliver by 15/12.\"}"
curl -X POST http://127.0.0.1:8000/messages/1/analyze
curl -X PATCH http://127.0.0.1:8000/cases/1 -H "Content-Type: application/json" -d "{\"expected_version\": 1, \"corrections\": {\"deadline\": [\"2026-12-15\"]}, \"note\": \"Date confirmed by phone.\"}"
curl -X POST http://127.0.0.1:8000/cases/1/approve -H "Content-Type: application/json" -d "{\"expected_version\": 2}"
curl http://127.0.0.1:8000/cases/1/audit
curl -X POST http://127.0.0.1:8000/messages/eml -H "Content-Type: message/rfc822" --data-binary @tests/data/samples/01_invoice_dispute_pl.eml
```
The Swagger UI is easier than curl.

### Corrections
`PATCH /cases/{id}` takes `corrections`: `{target: [values]}` where target is a field name
(`product`, `quantity`, `amount`, `deadline`, ...) or `category`; `null` reverts to the analyzer value;
`[]` means "there is no such value". Values are validated (dates `YYYY-MM-DD`, amounts `12500.00 PLN`,
quantities `50 pcs`, ...). Re-entering the analyzer's own value *confirms* it. Corrections go to the
append-only `case_corrections` table; the original message and the analysis row are never modified,
and the case view shows both the human value and the analyzer value.

### Approval checks
Before approval the **effective** data (analyzer output + corrections) is validated: category not
`other` and not still ambiguous, required fields present (order: product, quantity; complaint and
document request: reference; inquiry: product or reference), no unresolved ambiguous or invalid amount,
quantity or date, no unresolved date range, and no prompt-injection warning. An injection warning can
not be corrected away; it needs an explicit `override_reason` (min. 10 characters). The override and
the problems it covered are stored with the decision and in the audit event, together with a
snapshot of exactly what was approved. Approval performs no external action.

### Concurrency
Optimistic locking: every case has a `version`. Each mutation sends `expected_version`; if the case
moved on, nothing is written and the answer is 409 `version_conflict` with the current version, so a
stale form can never overwrite a newer edit. Each mutation is one short `BEGIN IMMEDIATE` transaction
holding the data change, the history rows and the audit event, with a compare-and-set
`UPDATE ... WHERE version = ?`. Double-submitting an approval gives one decision and one 409.
Imports are deduplicated by content (not by idempotency keys).

### Known security limitations (local demo)
* **No authentication or authorization.** Every change is attributed to the fixed label
  `local_demo_user`; this is a label, not proof of identity. The client cannot choose it: unknown
  request fields (`actor`, `reviewer`, `status`, ...) are rejected with 422 and headers are ignored.
* The server binds to `127.0.0.1`; `Host` headers other than localhost / 127.0.0.1 are refused
  (DNS-rebinding defence; IPv6 literals are not supported). No CORS headers are ever sent, and the
  `.eml` upload accepts only non-"simple" content types, so a web page cannot trigger imports
  cross-site. Any local process on your machine can still use the API.
* Do not expose the API to a network: no TLS, no rate limiting, no user separation.
* Request bodies are size-limited (transport and parser limits) but there is no per-client quota.
* Stored emails are returned verbatim in JSON; a future UI must escape them.
* The audit trail is an ordinary table: anyone with database access can edit it. Corrections and
  review rows are protected by append-only triggers; `audit_events` (a Stage 2 table) is not.

### Database
Tables are created with `CREATE TABLE IF NOT EXISTS` semantics on startup. Stage 4 only **adds**
`cases`, `case_corrections` and `case_reviews` (plus triggers); existing messages, analyses and
audit events are never altered, deleted or recreated. **Migrations are not implemented**: a future
change to an existing table needs a real migration tool; back up `data/local/*.db` before upgrading.
Timestamps are stored as UTC.

## Stage 5: Streamlit user interface

Two processes, started in two terminals (Windows, from the project folder):

```
:: terminal 1 - the backend (FastAPI)
.venv\Scripts\python -m ai_ops_hub.api --db data/local/demo.db

:: terminal 2 - the user interface (Streamlit)
.venv\Scripts\python -m ai_ops_hub.ui
```
Open **http://127.0.0.1:8501**. The sidebar shows whether the backend is reachable. Another backend
address: `python -m ai_ops_hub.ui --api-url http://127.0.0.1:8123` (or the `AIOPS_API_URL` environment
variable); only loopback addresses are accepted. A walkthrough in Polish is in
[docs/DEMO_PL.md](docs/DEMO_PL.md).

| screen | what it does |
|---|---|
| Panel | exact counts of all / needs_review / ready_for_review / approved / rejected cases (`GET /cases/stats`) |
| Import | `.eml` upload or pasted text; optional rule-based analysis; clear new / duplicate / conflict messages |
| Przypadki | case list with status filter, subject/sender search and pagination (one page is fetched at a time) |
| Szczegóły przypadku | the original email, extracted fields with source fragments, warnings, human corrections, review note, approve / reject / reopen, audit history |
| Wiadomości | all messages; analyze the ones without a case; conflicts are marked |

### Architecture rules of the UI
* **HTTP only.** `ai_ops_hub/ui/client.py` is the single place that talks to the backend
  (`httpx2`). The UI imports no repositories, services or database code and has its own small
  response models (`ui/models.py`), so it depends only on the JSON contract. The client has timeouts,
  turns failures into typed errors (`BackendUnavailable`, `VersionConflict`, `ApprovalBlocked`, ...),
  never retries a write, ignores proxy variables, does not follow redirects and refuses non-loopback
  addresses. Nothing is sent to external servers; Streamlit telemetry is off (`.streamlit/config.toml`).
* **No duplicated rules.** Allowed actions, problems, validation of corrected values and state
  transitions all come from the API; the UI only displays them.
* **Writes happen only on a button click.** Streamlit re-runs the whole script on every interaction,
  but a button is true for one run only, so a selected file, typed text or pending dialog is never
  submitted by a rerun. Approval, rejection and reopening need a second, explicit confirmation.
* **Optimistic locking.** Every write sends the case version the user was *looking at* (not the one
  fetched during the current rerun). On HTTP 409 `version_conflict` the UI shows what happened,
  reloads the newest state, refreshes the fields the user did not touch, **keeps the user's unsaved
  edits** and overwrites nothing. A banner also warns when a case changed after it was opened.
* **Validation warnings are never silently overridden.** If the case has problems (or the server
  refuses approval), approval needs a written reason (min. 10 characters) plus a checkbox, and both
  are stored with the decision.
* **Untrusted text.** Email content, extracted values and server messages are shown with
  `st.text` / `st.text_area` / `st.dataframe`, or escaped before any markdown widget. No
  `unsafe_allow_html`, no images or links from emails.
* **Three things kept visibly apart:** the original message, the values from the `rule_based`
  analysis, and the human corrections. The UI says everywhere that the analysis is pattern matching,
  not AI.

### API additions for the UI (backward compatible)
`GET /cases/stats` (exact counts, independent of pagination), `GET /cases?q=` (subject/sender
search) and `imported_at` in case summaries. Existing responses only gained fields.

### UI limitations
* No authentication (see the Stage 4 limitations); anyone who can open the page acts as `local_demo_user`.
* Unsaved edits survive reruns, conflicts and visits to other screens, but **not** a browser reload
  or a new browser tab (Streamlit session state lives in the server for one browser session).
* The case list refreshes when you interact with the page; there is no live push, so use the
  "Wczytaj aktualny stan" button or navigate to see other people's changes.
* Search is a substring match on subject and sender only (no body search, no category filter).
* The UI is Polish only. Large lists are paged (15 per page by default); there are no exports.
* Corrected values must use the formats the API accepts (`2026-12-15`, `12500.00 PLN`, `50 pcs`).
* The UI is tested with Streamlit `AppTest` against the real API in memory and with mocked HTTP;
  real browser behavior (layout, uploader widget) is only covered by the manual checks in
  [docs/DEMO_PL.md](docs/DEMO_PL.md).

## Setup and tests
```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```
Tests need no internet and no API keys (the API tests use FastAPI's `TestClient` on temporary databases).
