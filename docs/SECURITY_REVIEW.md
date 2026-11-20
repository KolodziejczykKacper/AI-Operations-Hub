# Security and privacy review

Scope: the whole repository at the end of Stage 8 (parsers, persistence, rule-based analysis, FastAPI, Streamlit, Excel export,
optional Claude adapter). Method: code review, targeted experiments, automated tests, `bandit` (static analysis) and `pip-audit`
(dependency advisories). This is a **self-review of a local demo**, not an independent penetration test, and it does not make the
project production-ready. No real Anthropic API request was made during the review.

## Threat model in one paragraph

A single user runs the backend and the UI on their own machine, with **fictional emails**. The assets are the local SQLite file, an
optional Anthropic API key and the user's money (paid API calls). The realistic attackers are: a hostile email (content is untrusted),
a hostile web page open in the same browser, a mistaken or poisoned environment variable, and another process on the same machine.
Remote network attackers are out of scope because everything binds to `127.0.0.1` (and refuses otherwise). There is **no
authentication**, by design for this demo.

## Findings by severity

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | **Medium** | The Anthropic SDK obeys `ANTHROPIC_BASE_URL`; with an explicit API key it sent the **key and the email** to whatever host that variable names (reproduced offline with a mocked transport). | **Fixed.** `make_client` always passes an explicit `base_url` (default `https://api.anthropic.com`); the variable is ignored. A different address needs `AIOPS_CLAUDE_BASE_URL` (https only, no credentials or query) **and** `AIOPS_CLAUDE_ALLOW_CUSTOM_BASE_URL=true`; look-alike hosts are rejected; the consent screen and the status endpoint name the destination host and warn loudly about a custom one. 20 regression tests. |
| 2 | **Medium** | `--allow-remote` could expose the unauthenticated API, including the paid Claude endpoint (its consent is part of the request body, so any caller can supply it). | **Fixed.** The API refuses any non-loopback address while Claude analysis is enabled, even with `--allow-remote`. Tests added. |
| 3 | **Low** | Browser CSRF: a hostile web page could make the browser send state-changing requests to `127.0.0.1:8000` (JSON endpoints were already protected by CORS preflight, but body-less POSTs such as `/messages/{id}/analyze` are "simple requests"). | **Fixed.** Middleware refuses `POST/PUT/PATCH/DELETE` whose `Sec-Fetch-Site` is `cross-site` or `same-site`. Non-browser clients (Streamlit server, curl, scripts) are unaffected. It is a guard against hostile pages, not authentication. |
| 4 | **Low** | No authentication: any local process or user who can reach the port can read data, approve cases (as the fixed label `local_demo_user`) and request a paid analysis. | **Accepted and documented.** Mitigations: loopback only, trusted-host check (DNS rebinding), per-request cost guard, 24-hour reserved budget, size limits. Never expose the API to a network. |
| 5 | **Low** | Local budget is a safety guard, not a spending limit: estimates are approximate, prices change, thinking tokens are not modelled. | **Accepted and documented.** Set a billing limit in the Anthropic Console. |
| 6 | **Low (dev only)** | `pip-audit` reports PYSEC-2026-1845 for `pytest 8.4.2` (fixed in 9.0.3). Test runner only; not part of the runtime. | **Mitigated.** The `dev` extra now requires `pytest>=9.0.3,<10`; the whole suite was run with 9.0.3 in a clean environment and passes. |
| 7 | **Info** | `bandit`: 0 high, 0 medium; 12 x B101 (`assert` used for internal invariants) and 1 x B105 (a regular expression flagged as a "password": false positive). | Reviewed; no action. |
| 8 | **Info / privacy** | The SQLite file stores full email bodies unencrypted; it is under `data/local/`, which Git ignores. Excel exports contain approved business data. | Documented. Use fictional data; do not commit `data/`. |
| 9 | **Info / privacy** | An early commit contains a test that lists the name of a former/current employer as a string to assert it is absent from the sample data (removed in the working tree in Stage 8). It is still in the Git history. | Owner's decision before publishing (see the release checklist). |

## What was checked and held up

* **Secrets and private data.** No real key-like string in the working tree or in any of the 8 commits (scan of full history). `.env`
  and `.env.*` are ignored; `.env.example` has no value. The API key is a `SecretStr`, never returned by any endpoint, never logged,
  never stored; tests assert it is absent from logs, the database, audit events and error responses. All authors/committers use the
  GitHub noreply address.
* **Malicious email content.** Size limits (file, text, MIME parts, headers), duplicate/conflict handling by hash, HTML converted to
  text without fetching anything, instruction-like text ("ignore previous instructions", "approve this case") detected and flagged,
  values inside it ignored. The UI renders email text with `st.text`/`st.dataframe` or escapes it; `unsafe_allow_html` is never used.
* **Spreadsheet formula injection.** Every untrusted cell is written as an explicit string type; a leading `= + - @ |` (also after
  invisible characters) gets an apostrophe; no hyperlinks, external references or macros; the raw XML is checked in tests. Only `.xlsx`
  exists: CSV safety has **not** been designed or verified.
* **Consent before external transmission.** Disabled by default; preview is local; the paid call needs a consent bound to the message,
  the content fingerprint, the model and the accepted cost; one fresh consent per request in the UI; nothing is sent on import, rerun
  or navigation. Only subject and body are sent (no sender, recipients, headers).
* **Concurrency and cost.** SQLite writes use `BEGIN IMMEDIATE`; the Claude budget is reserved atomically before sending; an identical
  second request for the same message is refused while the first runs; two simultaneous requests cannot spend the same remaining budget
  (thread tests). SDK `max_retries=0`.
* **Human control.** Nothing is approved automatically (a Claude analysis creates no case and no decision); every decision is a human
  action with a written override when problems remain; history and audit events are append-only (database triggers).

## Not covered

No authentication/authorisation, no encryption at rest, no rate limiting beyond local guards, no dependency pinning with hashes, no
fuzzing of the e-mail parsers beyond the included samples, no test against the **real** Anthropic API, no automated test in
Microsoft Excel (the owner made one manual check that an export displays correctly; see the README), no accessibility review of the UI.
