# Release checklist (before making the repository public)

Items marked **owner** need a decision or an action that only the repository owner can take. Nothing here has been done on GitHub:
no remote is configured by this project's tooling and nothing has been pushed.

## 1. Decisions only the owner can make

- [ ] **License (owner).** No `LICENSE` file exists, so the default is "all rights reserved". For a portfolio project the usual choices
      are MIT or Apache-2.0 (permissive). Dependencies are permissive (MIT, BSD-3-Clause, Apache-2.0): pydantic, FastAPI, SQLModel,
      SQLAlchemy, openpyxl, anthropic (MIT), uvicorn, starlette, httpx2 (BSD-3-Clause), streamlit (Apache-2.0). Add the license text and
      a `license` field to `pyproject.toml`. Sample data is fictional and original to this repository.
- [ ] **Git history and the employer-name string (owner).** An early commit contains a test with the name of a (former/current) employer
      as a string asserting that it is absent from the sample data. The working tree no longer has it, the history still does. Options:
      (a) accept it; (b) publish a fresh repository with a single squashed "initial" commit (history rewrite is the owner's call:
      this project's rules forbid rewriting existing commits); (c) rewrite history with `git filter-repo` before the first push.
- [ ] **Repository name, description, topics, visibility.** Suggested description: "Local human-in-the-loop email-to-record workflow:
      rule-based extraction, review API, Streamlit UI, Excel export, optional Claude analysis (portfolio project)".
- [ ] **Author identity.** All commits use the GitHub noreply address (checked). Confirm that the display name is the one you want public.
- [ ] **Personal data in screenshots/docs.** Screenshots in `docs/images/` use fictional data and `127.0.0.1`; look at them once yourself.

## 2. Verification the owner should run once

- [ ] Fresh clone on a clean machine or folder, then the README install steps, `pytest -q`, start both processes, run
      `scripts/demo_api.py` (documented clean-install result: see the Stage 8 report).
- [ ] Open a downloaded export in **Microsoft Excel** (never done by automated tests) and check that text such as `'=HYPERLINK(...)`
      shows as text and that dates and amounts are formatted as expected.
- [ ] **Optional authorized live Claude test** (the only way to remove the "never called for real" caveat): see the procedure in
      `docs/TECHNICAL_NOTES.md` (Stage 7). Set an Anthropic billing limit first; use one fictional email and the Haiku model. After it,
      update the README sentence "never been called for real" to describe what was actually observed (model, date, cost).
- [x] `pytest` advisory (dev-only, flagged in 8.4.2): the `dev` extra now allows `pytest>=8,<10`; the suite passes with 9.0.3.

## 3. Repository hygiene (checked in Stage 8)

- [x] `.gitignore` covers `.env`, `.env.*` (but `.env.example` is tracked), databases, `data/local/`, exports, caches, virtual envs.
- [x] No secrets or key-like strings in the working tree or in the full history; no binaries except four small PNG screenshots.
- [x] Dependencies declared with compatible-release bounds in `pyproject.toml`; all licenses permissive; no unused heavy libraries.
- [x] Commit messages are conventional (`feat:`, `docs:`), one logical change per commit, authored with the noreply address.
- [ ] Optional: a `CONTRIBUTING`/`SECURITY.md` pointer to `docs/SECURITY_REVIEW.md`, a CI workflow running `pytest` (no secrets needed),
      `.gitattributes` with `* text=auto` (Git currently warns about LF/CRLF on Windows).

## 4. Documentation review (checked in Stage 8)

- [x] README states that it is a local demo, has no authentication, uses fictional data and measured quality only on self-written
      datasets; it separates simulated from real API verification and lists limitations.
- [x] No claim of production readiness, real-world accuracy or deployment experience.
- [x] Windows install steps reproduced in a clean virtual environment from a snapshot of the repository.

## 5. Final pre-publication commands

```
git status                      # must be clean
git log --format="%h %an <%ae> %s"   # every author line must be the noreply address
git grep -nE "sk-ant-[A-Za-z0-9_-]{16,}"   # must print nothing except the documented fake test key
.venv\Scripts\python -m pytest -q
git remote add origin <url>     # only when you have decided on everything above
git push -u origin main         # only by the owner
```
