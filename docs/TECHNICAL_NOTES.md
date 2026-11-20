# AI Operations Hub: technical notes

Detailed, stage-by-stage technical notes (persistence, analysis, API, UI, export, optional Claude analysis).
The project entry point is the [README](../README.md); this file keeps the long form for readers who want the details.
Everything here describes a **local demo with fictional data**; see the README for the honest list of limitations.

All sample data in `tests/data/samples/` is fictional (reserved `.example` domains).

## Architecture rules (approved at the start of the project)
1. The Streamlit UI talks to FastAPI over HTTP only (no repositories, services or SQLite in the UI).
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
* The UI is Polish only. Large lists are paged (15 per page by default). Excel export: see Stage 6.
* Corrected values must use the formats the API accepts (`2026-12-15`, `12500.00 PLN`, `50 pcs`).
* The UI is tested with Streamlit `AppTest` against the real API in memory and with mocked HTTP;
  real browser behavior (layout, uploader widget) is only covered by the manual checks in
  [docs/DEMO_PL.md](docs/DEMO_PL.md).

## Stage 6: Excel export of approved cases

A nontechnical user can export approved cases to a `.xlsx` workbook from the Streamlit screen
**Eksport do Excela** (or call the API directly). It is a read-only feature: nothing is sent anywhere
and no case changes.

### How to use it
1. Start the backend and the UI as in Stage 5 and open http://127.0.0.1:8501.
2. Approve at least one case (screen *Szczegóły przypadku*).
3. Open **Eksport do Excela**. The screen shows how many approved cases are eligible. Optionally
   limit the export to an approval-date range.
4. Click **Wygeneruj plik Excel**, then **Pobierz plik Excel (.xlsx)**. The file is generated only when you
   click; reruns of the page and the download click do not call the API again.

### What is exported
* **Only cases whose current status is `approved`.** Rejected, `needs_review`, `ready_for_review` and
  conflicting imports are excluded. A **reopened** case is open again, so it leaves the export until it
  is approved again (then its new approval is exported).
* **Values come from the approval snapshot**, i.e. what the human approved, human corrections
  included. The snapshot is cross-checked against the case's effective data; a case whose records
  disagree is left out and counted in the *Informacje* sheet and on the screen (never exported on trust).
* Nothing is invented. A value the case does not state is an **empty cell**. A value the rule-based
  analyzer was unsure about (`ambiguous` / `invalid`) that no human confirmed is written as text
  `[niepotwierdzone] <value>` on a yellow background and listed in *Pola do weryfikacji*.
* Overrides are visible: *Zatwierdzono mimo problemów* = TAK, the written justification and the list of
  overridden problems. Human corrections are listed as `field: analysis "x" -> human "y"`.

### Workbook structure
Sheet **Zatwierdzone przypadki** (one row per case, ordered by case id; header row frozen, filter on,
widths set), columns in this fixed order:
ID przypadku, Kategoria, Nadawca, Temat e-maila, Data importu (UTC), Data zatwierdzenia (UTC),
Produkt / usługa, Ilość, Jednostka, Kwota, Waluta, Termin realizacji, Żądana data, Numer referencyjny,
Osoba kontaktowa, Organizacja, E-mail, Telefon, Notatka przy zatwierdzeniu, Pozostałe notatki,
Zatwierdzono mimo problemów, Uzasadnienie (override), Zastąpione problemy,
Pola do weryfikacji (niepotwierdzone), Poprawki człowieka, Wersja przypadku.

Dates are real Excel dates (`yyyy-mm-dd`, timestamps in **UTC**), amounts and quantities are numbers
(`#,##0.00`) when there is one confirmed, well-formed value with at most 15 significant digits; several
values are joined with `; `. Sheet **Informacje** records when the file was generated, the filter, the
counts and a legend. The analysis is described there as rule-based (not AI).

### Formula-injection protection
Email text, sender, subject, notes and human-typed corrections are untrusted. Every text cell is
written with an explicit *string* type, control and invisible characters (zero-width space, BOM,
bidi marks, tabs, NUL) are removed first so they can not hide a dangerous first character, and text
whose first visible character is `=`, `+`, `-`, `@` or `|` (also full-width forms) gets a leading
apostrophe. Plain signed numbers such as `+48600100200` are left alone. No hyperlinks, external
references or macros are ever written. Cells are clipped below Excel's 32 767-character limit. Tests
reopen the generated file with `openpyxl` and check cell types and the raw XML.

### Endpoints
| method and path | purpose | errors |
|---|---|---|
| `GET /exports/approved-cases/summary?approved_from&approved_to` | how many cases an export would contain now | 422 filter, 503 database |
| `GET /exports/approved-cases.xlsx?approved_from&approved_to` | the workbook (`Content-Disposition: attachment`, `Cache-Control: no-store`) | 409 `no_approved_cases`, 422 `export_too_large` / `invalid_export_filter` / `validation_error`, 500 `export_generation_failed`, 503 `export_database_error` |

`approved_from` / `approved_to` are UTC calendar days (`YYYY-MM-DD`), both inclusive. One export holds
at most **500 cases**; above that the API refuses (it never truncates silently) and asks for a narrower
date range. All reads happen in one database read transaction, so the file is a consistent snapshot even
if cases change while it is generated. Business logic lives in `services/export_service.py`; the route only
shapes the HTTP response.

### Export limitations
* **No export audit event.** Audit events belong to one message, an export spans many, and the server can
  not know whether a generated file was ever downloaded, so it records nothing rather than claim a download.
* No authentication: anyone who can open the UI can export (see the Stage 4 limitations).
* The file is a point-in-time copy; Excel edits are never imported back.
* **Only .xlsx exists.** There is no CSV export, and the formula protection above has not been designed or
  verified for CSV; a future CSV export needs its own safety work and tests.
* The 500-case cap and the (UTC) date filter are the only filters; no category or text filter.
* Values are exactly as the API stores them (`12500.00 PLN`, `2026-12-15`); no locale-specific
  currency formats. Opening in Excel depends on its locale for the decimal separator.
* Tested with `openpyxl` and (manually) the Streamlit UI; the file was not opened in Microsoft Excel itself
  in the automated tests. Separately, the repository owner opened one generated export in Microsoft Excel by hand and confirmed
  that it displays correctly (see the README).

## Stage 7: optional Claude API analysis

Two analyzers can look at the same email, and **you choose explicitly**:

| | `rule_based` (default) | Claude API (`llm`, optional) |
|---|---|---|
| how | deterministic patterns, offline | a language model through the official Anthropic SDK |
| cost | free | **paid**, per request |
| data leaves your computer | never | **yes: subject and body go to Anthropic** |
| when it runs | import + analyze button | **only** after a per-request explicit consent |
| stored as | `analyses` row, method `rule_based` | separate `analyses` row, method `llm`, version `<prompt>/<model>` |

Claude is **disabled by default and never called automatically**: not on import, not on page refresh,
not by the normal analyze endpoint, and not by the benchmark unless you authorize spending.

### Enable it (optional)
1. Create an API key in the Anthropic Console and **set a billing / spend limit there** (the local guards below
   are estimates, not a guaranteed spending limit).
2. Put the key and the switch in the environment or in the local, Git-ignored `.env` file (never in code):
   ```
   AIOPS_CLAUDE_ENABLED=true
   ANTHROPIC_API_KEY=sk-ant-...your key...
   ```
   Optional: `AIOPS_CLAUDE_MODEL` (default `claude-haiku-5-5`, the cheapest; `claude-sonnet-5-5` and `claude-opus-5-5` also have
   known prices), `AIOPS_CLAUDE_MAX_OUTPUT_TOKENS` (4000), `AIOPS_CLAUDE_MAX_INPUT_CHARS` (20000),
   `AIOPS_CLAUDE_MAX_COST_USD_PER_REQUEST` (0.02), `AIOPS_CLAUDE_DAILY_BUDGET_USD` (0.25), `AIOPS_CLAUDE_TIMEOUT_SECONDS` (30).
3. Restart the backend. The Streamlit screen **Analiza Claude** shows the status (never the key).

**Offline mode** is simply not enabling it: everything else, including comparison of existing analyses, keeps working.

### What is sent, and what it costs
* Sent: the **subject and body** of ONE email, plus fixed instructions and the JSON schema (about 1.5K input tokens).
  **Not** sent: sender address, recipients, headers, other emails, your database. No tools, no web search, no
  files, no streaming. Do not use real, personal or confidential emails for demos.
* Prices (USD per 1M tokens, verified 2026-11-17 at platform.claude.com/docs/en/about-claude/pricing; they can change):
  Haiku 5.5 $0.10 in / $0.50 out (above 100K prompt tokens $0.50 / $2.50), Sonnet 5.5 $2 / $10, Opus 5.5 $4 / $20.
  A typical email costs about $0.0003 with Haiku, $0.006 with Sonnet, $0.012 with Opus.
* Before each request the screen shows the model, the estimated tokens and a **worst-case cost** (input estimate plus the whole
  output cap). After it, the **actual** cost computed from the tokens the API returned. Local estimates are NOT a billing record.

### Consent and cost controls
* `GET /messages/{id}/claude/preview` (local, free, no network), then `POST /messages/{id}/claude/analyze` with
  `{"confirm_send_to_anthropic": true, "model": "<the previewed model>", "accepted_max_cost_usd": <at least the estimate>}`.
  Wrong or missing consent: 422, nothing sent. In Streamlit: tick the consent box, then click the paid button; the box is
  new (unticked) after every click, and reruns never send.
* Checks in order, all BEFORE any network call: enabled, key present, model has a known price, message exists and is not a
  conflicting import, **already analysed -> returns the stored analysis and sends nothing**, input size, per-request cost
  guard, an early look at the 24-hour budget, consent, and finally the **atomic reservation** (next bullet).
* **The consent is bound to exactly this request:** the `message_id`, the content fingerprint of the exact text you saw in the
  preview, the model and an accepted cost at least equal to the worst-case estimate. Change any of them and the request is refused
  (422) until you fetch a new preview.
* **Budget reservation:** before anything is sent, one `BEGIN IMMEDIATE` transaction re-reads the 24-hour spend (running requests
  included) and inserts a `reserved` usage row holding the worst-case estimate, so two concurrent requests cannot both spend the same
  remaining amount. The row is settled in place (`ok`/`failed`, actual cost from usage). The same step refuses (409
  `claude_in_progress`) a second identical request for a message while the first one is running. A crashed request stays
  `reserved`: it keeps counting for 24 hours, so the guard errs on the safe side. **This is a local guard, not a guaranteed spending
  limit on your Anthropic bill.**
* The SDK client is built with `max_retries=0` and a timeout: a paid request is **never repeated automatically**; a failed one
  can only be repeated by a new explicit consent. Billing for failed or truncated answers is possible; the usage is recorded.

### Safe structured extraction
* Schema-constrained JSON (`output_config.format`). The email is untrusted DATA between tags; the system prompt forbids following
  instructions in it; the request has no tools, so the model has nothing to trigger. The answer can only add values to ONE
  analysis: it can not approve, create a case, write files or call anything.
* The model returns the category and, for each field, a verbatim **quote**, the normalized value and a status. **Offsets are computed
  locally** by finding the quote in the original text, so spans cannot be hallucinated, and every span is verified.
* Where the span goes: offsets are found locally; see the limitation about repeated quotes below.
* A value is **rejected** (a `llm_value_rejected` warning) when the quote is not in the text, when the normalized value is not
  supported by the quote (different digits, a currency or year the text does not state, rewritten names), or when it sits inside
  instruction-like text. A year that is not written becomes `--MM-DD` and `ambiguous`; a currency that is not written is not assumed.
  The model's status can only be lowered by local checks. Review reasons and prompt-injection detection are computed locally with the
  same rules as for `rule_based`.

### Persistence, cases and comparison
* A Claude result is a **separate, versioned analysis** (`analysis_method = llm`; another model or prompt version is another row).
  Existing analyses, corrections and approved/rejected cases are never changed. The new table `llm_calls` (additive; created on start)
  stores tokens, cost, latency, request id and counters, **never prompts, answers or email text**. Success writes an audit event
  (`analysis_completed`, actor `human`); a failure writes `analysis_failed` and no analysis.
* **An existing case keeps its analysis.** Adopting another analysis for an open case would need history changes, so Stage 7 offers
  only a **read-only comparison** (`GET /messages/{id}/compare`, screen *Analiza Claude*).
* For a message **without a case** you can create the case from the analysis you choose: `POST /messages/{id}/case` with
  `{"analysis_id": ...}` (409 if a case already exists). Without an explicit choice a new case always reviews the latest `rule_based`
  analysis, never a Claude one. A case reviewing a Claude analysis is labelled `llm` everywhere (detail screen, audit, Excel
  column *Metoda analizy*).
* Conflicting imports are never analysed, by either analyzer.

### Evaluation (offline by default)
`python -m ai_ops_hub.analysis.claude.benchmark tests/data/eval/dataset_v1.jsonl` prints the `rule_based` baseline and a price
estimate (no network). `--simulate faithful|noisy` runs the real Claude pipeline against a fake provider: it checks the verification
layer (false rejections, caught fabrications) and says **nothing about Claude's quality**. Datasets are never pooled: the dev set
(`dataset_v1`), the `challenge` set and the `holdout` set are separate runs; do not tune on holdout. Live runs (`--live`) are refused
unless Claude is enabled, a key is set, `AIOPS_BENCHMARK_LIVE_AUTHORIZED=yes-i-authorize-spending` is set,
`--authorize-spending-usd` covers the worst-case estimate, and the holdout set additionally needs `--allow-holdout`.

### One authorized low-cost live test (only when YOU decide)
1. Set a spend limit in the Anthropic Console. Put `AIOPS_CLAUDE_ENABLED=true` and `ANTHROPIC_API_KEY` in `.env`.
2. Start the backend and the UI. Import ONE fictional email (the example in `docs/DEMO_PL.md`) and run the `rule_based` analysis.
3. Open **Analiza Claude**, check the preview (about $0.002 worst case with Haiku), tick the consent box, click the paid button once.
4. Read the actual cost and the comparison; the Anthropic Console shows the real charge.
5. Optional: `set AIOPS_BENCHMARK_LIVE_AUTHORIZED=yes-i-authorize-spending` and
   `.venv\Scripts\python -m ai_ops_hub.analysis.claude.benchmark tests\data\eval\dataset_challenge_v1.jsonl --live --max-cases 5 --authorize-spending-usd 0.05`.

### Endpoint safety (Stage 8)
* The API key is only sent to `claude_base_url` (default `https://api.anthropic.com`). The SDK would otherwise obey the
  `ANTHROPIC_BASE_URL` environment variable, so that variable is ignored. A different address needs `AIOPS_CLAUDE_BASE_URL` (https,
  no credentials) plus `AIOPS_CLAUDE_ALLOW_CUSTOM_BASE_URL=true`; the UI names the destination host and warns about a custom one.
* The API refuses to listen on a non-local address while Claude analysis is enabled (even with `--allow-remote`), and refuses
  state-changing requests that a browser marks `Sec-Fetch-Site: cross-site` or `same-site`.

### Stage 7 limitations
* The local token estimate is pessimistic but approximate; thinking tokens, price changes or SDK behavior can make real costs differ.
  There is no hard spending limit in this code: use the Console limit.
* **No real API request was made while developing this stage.** The prompt, the exact `output_config` combination (`effort` with
  `format`) and the real model's quality are untested against the real API until your authorized test.
* If the quoted text of an **amount, quantity or date** occurs more than once (in one source, or in the subject AND the body), the
  field is kept at its first occurrence but marked `ambiguous` with a `llm_span_ambiguous` warning, because each occurrence may
  have a different business role. Repeated names, references, e-mails and phones are the same value and are not flagged.
* Claude may miss fields or choose another category than `rule_based`; the comparison shows it, a human decides.
* No authentication and no per-user budgets; the daily budget is a local sum over recorded estimates.
* Only the three listed models have known prices; others are refused.

## Setup and tests
```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```
Tests need no internet and no API keys (the API tests use FastAPI's `TestClient` on temporary databases).
