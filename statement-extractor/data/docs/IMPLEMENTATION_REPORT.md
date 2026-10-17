# Implementation report: multilingual, AI-assisted bank statement extraction

**Scope.** This report covers the implementation of the "Master Prompt: Build a Complete,
Multilingual, AI-Powered Bank Statement Extraction Application" in the existing
`statement-extractor` codebase. It covers what was built, how it was verified, the results,
what could **not** be verified, and the remaining work. Test figures are from runs on
2026-10-09 on the development machine (Windows 11, Python 3.13.5, Node 20+).

## 1. Summary

| Area | Before | After |
|---|---|---|
| Engines | Local deterministic engine only | `local` (default), `groq` (`openai/gpt-oss-20b`, strict JSON schema) and `hybrid`, chosen per server and per upload, with visible fallback |
| Completeness | Not assessed; "pipeline finished" looked like success | `complete` / `partial` / `needs_review` / `failed` from evidence, with coded reasons and metrics |
| Validation | Running balance, statement totals, direction recovery | Plus newest-first detection, duplicate detection, coded messages, per-account ledgers, unresolved-line capture, a confidence evidence signal |
| Processing | Synchronous request; the UI animated a fake progress list | Background jobs (no Redis), real stage and progress, interrupted-run recovery, in-flight dedupe |
| Review | Read-only results page | Edit, add, delete, restore and confirm rows and details; dismiss lines; mark duplicates genuine; audit trail; frozen originals; immediate revalidation |
| UI language | English only, Google Fonts | en / es / fr / de / ar, RTL, locale formatting of exact decimal strings, system and Noto fonts (no external requests) |
| Security | No auth, unbounded upload read, CSV unescaped | Bearer-token auth with owner isolation, bounded uploads, page limit, refusal of encrypted PDFs, CSV formula neutralisation, retention, no-store, error codes without echo or traces |
| Schema | `create_all` only | Versioned additive migrations with automatic backup; the existing database was upgraded in place |
| Tests | Scorecard script, fuzzer | Plus 62 pytest tests (1 live test, skipped unless enabled), 30 frontend tests, an engine-evaluation harness |

## 2. What was built, by phase

### Phase 1: foundation

- `app/core/config.py`: engine settings and safe defaults (`EXTRACTION_ENGINE=local`,
  `ALLOW_EXTERNAL_LLM_CALLS=false`). `engine_problems()` gives stable reasons an engine cannot
  run: `external_calls_disabled`, `groq_key_missing`, `groq_model_unsupported`,
  `groq_sdk_missing`. Only `openai/gpt-oss-20b` is accepted.
- `app/models/models.py` + `app/db/migrations.py`: schema version 2, additive only. New tables
  and columns:
  - `corrections` (new table);
  - `documents.owner`;
  - run engine, stage, progress, LLM usage and chunk columns;
  - extraction completeness and corrected flags;
  - field and transaction account, provenance, evidence, codes and originals;
  - transaction `amount` (direction unknown) and `deleted`.

  Before migrating, the database is backed up with SQLite's online backup API.
- `app/core/messages.py`: every user-facing message is `code + params + English text`.

### Phase 2: completeness and reliability

- `validation_service.py` (rewritten):
  - newest-first detection, by dates or by which order reconciles; the chain is checked
    chronologically;
  - duplicates are flagged, except where the running balance proves both rows real;
  - coded issues and checks; field checks for dates, currency and unnormalised values;
  - subset-sum direction recovery and hints kept.
- `table_service.py`: lines that look like transactions but became no row are captured as
  `unresolved`: after the table end, outside any row, or detached.
- `completeness_service.py`: the status rules (README, "Completeness") and per-account
  metrics.
- `confidence_service.py`:
  - an evidence signal (PDF text, OCR, repaired OCR, LLM verified or unverified, user) with a
    0.15 weight;
  - caps for unverified values (0.6), mismatches (0.55) and issues (0.65);
  - coded reasons.

### Phase 3: Groq integration

- `schemas/llm.py`: the strict output schema. Values are copied as printed, with cited line
  ids. A transform makes it Groq-strict: all properties required, closed objects, refs inlined.
- `page_text.py`: numbered, layout-preserving lines (`p3.l17`). An amount stays under its
  column heading even when the neighbouring cell is empty. This was found necessary during
  testing: joining cells with separators made debit and credit indistinguishable.
- `llm_client.py`:
  - `GroqClient`, built only when allowed, sends: temperature 0, seed, `reasoning_effort`,
    `include_reasoning=false`, `max_completion_tokens`, strict `json_schema`;
  - typed errors with no content;
  - a model-identity check;
  - `ScriptedClient`, `ReplayClient` and `RecordingClient` for tests and evaluation.
- `llm_extraction_service.py`:
  - chunking with context and account registry;
  - repair, truncation-split and rate-limit handling;
  - verification of dates, amounts, descriptions and identifiers against cited lines, using
    the deterministic parsers;
  - mapping to the same domain objects as the local engine;
  - the omission check; injection-safe prompt construction.

### Phase 4: hybrid

- `pipeline.py`:
  - picks the result the arithmetic proves (ties go to `local`) and records disagreements;
  - fills failed Groq chunks with local rows marked `source_engine=local`;
  - falls back visibly (`engine_used`, `fallback_reason`, a warning check);
  - the `groq` engine fails rather than falls back.

### Phase 5: API and UI

- API (`api/v1/documents.py`, `api/v1/system.py`):
  - upload (background or `wait`), status, list, result;
  - field and transaction edits, create, delete/restore, dismiss, revalidate, reprocess;
  - corrections, CSV/JSON export, preview, delete, config, health.
- Frontend (rewritten): hash routes (upload, live processing, review, history, settings).
  - **i18n:** `src/i18n`, with five dictionaries of identical keys and placeholders (enforced
    by test).
  - **Formatting:** placeholders are typed (money, date, number, fields, checks); amounts are
    formatted from decimal strings via BigInt. RTL works through `dir` on `<html>`, logical
    CSS properties, LTR-isolated amounts, and `bdi dir=auto` for original text.

### Phase 6: security and operations

- Token auth with owner isolation (404 for others' documents) and constant-time token
  comparison (`core/security.py`).
- Bounded upload read (413), page limit, magic-byte type check, refusal of encrypted PDFs.
- CSV formula neutralisation; `Cache-Control: no-store`, `nosniff`, `no-referrer`.
- Validation errors list field names only; the 500 handler hides internals.
- Retention purge thread; explicit child-first deletion of a document's data and file.
- Startup checks: config problems logged by code, interrupted runs marked failed.

### Phase 7: verification and documentation

- Tests (section 3), migration of the real local database (section 4), `README.md`,
  `docs/DEPLOYMENT.md`, this report, `backend/.env.example` (placeholders only).

## 3. Verification results

| Check | Result |
|---|---|
| `pytest tests` (backend) | **62 passed, 1 skipped** (the live Groq test; needs `RUN_LIVE_GROQ=1` + key) |
| Scorecard `tests/test_pipeline.py` (7 real company statements, 3 mocks, 28 corpus) | **38/38** |
| Fuzzer `tests/fuzz.py 1000` | **1000/1000** |
| Frontend `npm test` (vitest) | **30 passed** |
| Frontend `npm run build` (tsc strict + vite) | passes |
| End-to-end on the running server, synthetic statement | Upload 202 → stages `reading → analyzing → done` → `needs_review` (row missing) → reviewer adds the row → `complete` → CSV → audit → delete 204 |

What the pytest suite covers:

- **Local engine:** clean statements; newest-first; duplicated line (flagged, kept); genuinely
  repeated payments (not flagged); omitted line; statements without a running balance; a
  wrong printed closing balance; empty statements; multi-page statements; page failure →
  `partial`; confidence caps.
- **Groq and hybrid (scripted):**
  - strict schema; chunk budget, context and coverage; data-block breakout;
  - the happy path; invented amount; skipped row → `not_cited`; invalid citation;
  - prompt injection inside the statement (it reaches the model only inside the data block,
    and the forged row stays unverified and unreconciled);
  - truncation split; one repair without echo; rate-limit wait and budget exhaustion;
  - fatal auth → the `groq` engine fails;
  - partial chunk failure → `partial`; hybrid gap-fill; hybrid fallback; hybrid
    agreement and disagreement;
  - two accounts in two currencies validated separately;
  - the exact parameters sent to the Groq SDK; model-substitution rejection;
  - no statement content in logs.
- **API:**
  - health/config without secrets; background processing with real progress; engine refusal;
    the Groq engine via the API; failed Groq run recorded;
  - the review workflow with audit and revalidation; field normalisation with document
    conventions; duplicate resolution; unresolved dismissal;
  - CSV injection and corrections in exports; deletion of data and file;
  - type, size, page and password limits; no input echo; token isolation; interrupted-run
    recovery.
- **Migrations:** a legacy v1 database upgraded in place, data preserved, backfilled,
  backup made, idempotent; a fresh database; legacy results never presumed complete.

### Engine evaluation (`tests/evaluate_engines.py`)

**Synthetic scenarios** (7): clean, newest-first, no balance, duplicated line, omitted line,
empty, 60-row multi-page. The groq/hybrid columns use the **simulated** model.

| metric | local | groq (simulated) | hybrid (simulated) |
|---|---|---|---|
| header accuracy | 1.00 | 1.00 | 1.00 |
| transaction precision / recall | 0.9917 / 0.9917 | 0.9917 / 0.9917 | 0.9917 / 0.9917 |
| direction accuracy | 1.00 | 1.00 | 1.00 |
| duplicated / omitted rows | 1 / 1 | 1 / 1 | 1 / 1 |
| reconciliation rate | 0.71 (5 of 7) | 0.71 | 0.71 |
| tokens in / out | 0 | 7000 / 2800 (scripted counts) | 7000 / 2800 |

The single duplicated and single omitted row are the scenarios that print them on purpose.
The engine correctly reports both as `needs_review`. **The simulated column measures the
deterministic layer, not Groq.**

**Local engine on the generated corpus and mocks plus the scenarios** (32 documents):

- header accuracy 0.9956;
- transaction precision and recall 0.9988, direction accuracy 1.00;
- 21 `complete`, 11 `needs_review`. The reasons: 9 have required details missing (corpus
  styles that print no label for some details, by design), 2 failed checks and 1 duplicate
  (the deliberate scenarios).

## 4. Migration of the existing database

`data/statements.db` was at the pre-versioning schema (v1): 3 documents, 3 runs,
3 extractions, 54 fields, 8 transactions. On the first start of the new backend:

- a backup was written, `statements.db.bak-v1-20261009T074843Z`;
- the database was migrated to v2;
- the counts afterwards were identical; `schema_migrations` = [1, 2];
- all three documents load through the new API as `needs_review` / `legacy_not_assessed`.

Revalidation of the legacy extractions was tested on a temporary copy, deleted afterwards. It
converts them to the new summary format and assesses completeness. The real database was not
revalidated: that stays a user action (**Check again**).

The first test copy, made with a plain file copy while the server ran, missed pages still in
the WAL. As a result, the migration backup now uses SQLite's online backup API, and the
deployment guide says how to back up a live database.

## 5. Live Groq results (added 2026-10-09, after a key was provided)

Only synthetic statements were sent; the confidential samples were not.

- **Basic call:** Groq reports the model exactly as `openai/gpt-oss-20b`, so the
  substitution check passes. Strict `json_schema` output works.
- **Live smoke test:** `tests/test_groq_live.py` passes.
- **Through the running API:**
  - `engine=groq`: 1 call, ~2.3K tokens in and ~1.3K out (incl. reasoning), ~3 s; 12/12 rows
    verified; `complete`.
  - `engine=hybrid`: the engines agreed; the local rows were kept.
- **First live evaluation** (7 synthetic scenarios) exposed model errors, all caught by
  verification and reconciliation:
  - header fields cited one line too early;
  - the statement period returned as "statement date";
  - salary credits put in the debit column (5 of 60 rows);
  - the same figure returned as both debit and amount;
  - an invented value date;
  - one intermittent `json_validate_failed`.

  Groq row recall was 0.48. Fixes:
  - a field is re-anchored to a line within ±2 only where the exact value is printed;
  - a model-assigned debit/credit is swapped back when the running balance proves it
    (local rows are only flagged);
  - a repeated amount is ignored, and a conflicting one flagged;
  - `json_validate_failed` is retried once with a changed prompt;
  - prompt rules for the period, statement number, value date and column position
    (prompt version 2026-10.2).
- **After the fixes:**

  | metric | local | groq | hybrid |
  |---|---|---|---|
  | header accuracy | 1.00 | 1.00 | 1.00 |
  | transaction precision / recall | 0.9917 / 0.9917 | 0.975 / 0.975 | 0.9917 / 0.9917 |
  | direction accuracy | 1.00 | 0.983 | 1.00 |
  | omitted rows (1 deliberate) | 1 | 3 | 1 |
  | complete (of 7; 2 are deliberately broken) | 5 | 4 | 5 |
  | tokens in / out (7 documents) | 0 | 16.9K / 11.3K | 16.9K / 11.6K |
  | seconds per document | 0.13 | 18 | 24 |

- **Remaining groq weakness:** in a table *without* a running balance, gpt-oss-20b still
  put two credits in the debit column. Reconciliation fails, so the statement is marked
  `needs_review`, and `hybrid` returns the correct local result. A possible next step is to
  recompute the model's directions from the books when the statement-level proof fails and
  the solution is unique.
- **Not measured:** rate-limit behaviour under load (no 429 occurred) and cost (set
  `GROQ_PRICE_*_PER_MTOK`).

## 5b. What was not verified (assumptions)

1. The Groq figures above come from 7 synthetic statements written by the same hand as the
   extractor. Accuracy on real banks' layouts is not measured.
2. The `retry-after` and `finish_reason="length"` paths have been exercised only with
   scripted clients.
3. **Token budgeting** uses a character-based estimate, not the model's tokenizer.
   Free-tier limits (about 8K tokens per minute) mean a long statement takes several
   minutes.
4. **The UI was not driven in a real browser.** It was verified by:
   - TypeScript strict build;
   - component tests in jsdom (filters, restore, translated checks, RTL `dir`, LTR amounts);
   - the API end to end.

   Visual layout, especially Arabic RTL, needs a human look.
5. **Translations** (es, fr, de, ar) were written for this release without review by native
   speakers.
6. **Real-statement coverage** for the local engine is still the 7 confidential samples. The
   synthetic corpus is the developer's own rendering of conventions.

## 6. Design decisions

- **The model locates; deterministic code decides.** Amounts and dates are parsed by the
  same parsers as the local engine. A value not found in its cited lines is flagged, never
  silently trusted. Completeness and reconciliation never depend on the model's opinion.
- **Direction is never guessed.** An unsigned amount without a direction marker stays an
  `amount`. Only the arithmetic (running balance, unique subset-sum) assigns direction, and
  only when the answer is unique.
- **Hybrid ties go to `local`.** Same proof means the result read directly from the PDF
  positions, with no external dependency, wins.
- **Stable storage position.** `row_index` is a storage key, and the displayed index is
  computed, so insertions and deletions never renumber stored rows. Validation messages use
  the displayed index.
- **Single process, no broker.** It meets "no Redis" and keeps operations simple. The cost
  is horizontal scale (DEPLOYMENT.md).

## 7. Requirement traceability

| Requirement | Status | Where |
|---|---|---|
| local / groq / hybrid engines, configurable | Done | `config.py`, `pipeline.analyze`, upload `engine` field |
| Env vars (EXTRACTION_ENGINE, GROQ_*, ALLOW_EXTERNAL_LLM_CALLS) | Done | `.env.example` |
| Schema-constrained output, Pydantic validation, repair | Done | `schemas/llm.py`, `llm_extraction_service._parse` |
| Source line ids, chunking, dedupe, ordering, failed-chunk tracking | Done | `page_text.py`, `make_chunks`, `ResultMapper`, `ChunkOutcome` |
| Source verification | Done | `amount_found`, `date_found`, `text_found`, `_value_verified` |
| Prompt-injection protection | Done (data block, rules, verification) | `SYSTEM_PROMPT`, `_sanitize`, test |
| No content logging; usage and latency recorded | Done | `llm_client.py`, `ProcessingRun` LLM columns, test |
| Multi-account and multi-currency, never summed | Done (groq); local flags suspicion | `AccountRegistry`, per-account validation |
| Provenance explicit / inferred / derived / unknown | Done | `TableRow.provenance`, `_field_row` |
| Decimal-safe money | Done | Decimal throughout; decimal strings in API and UI |
| Newest-first, duplicates, unresolved tokens, totals, balances | Done | `validation_service.py`, `table_service.py` |
| Completeness statuses | Done | `completeness_service.py` |
| Confidence reflects evidence | Done | `confidence_service.py` |
| Review workflow, audit, originals, revalidation | Done | `correction_service.py`, `pipeline.revalidate`, UI |
| Search / sort / filter / evidence view | Done | `TransactionsTable.tsx` |
| Real processing status; history | Done | `/status`, `ProcessingPage`, `HistoryPage` |
| i18n, selector, persistence, locale formatting, RTL, fonts | Done | `src/i18n`, `AppHeader`, `index.html`, `tailwind.config.js` |
| Backend error codes and params; originals never translated | Done | `messages.py`, `_error`, `Original` component |
| Upload, status, results, edit, corrections, revalidate, export, list, delete, health | Done | `documents.py`, `system.py` |
| Auth / tenant isolation, CORS, limits, CSV protection, retention, timeouts, no traces | Done | `security.py`, `main.py`, `jobs.py`, `result_service.csv_safe` |
| Background processing without Redis; accurate failure recording | Done | `jobs.py` |
| pytest with mocked LLM, no live key in CI | Done | `tests/` |
| Frontend tests | Done | `src/**/*.test.ts(x)` |
| Engine-comparison harness with the listed metrics | Done; live Groq numbers pending a key | `tests/evaluate_engines.py` |
| Additive migrations, existing DB intact | Done and applied | `migrations.py`, section 4 |
| README, deployment guide, report, .env.example | Done | this folder |

## 8. Recommended next steps

1. Run the live smoke test and the live evaluation on synthetic statements with a Groq key.
   Then record answers (`--record`) so they can be replayed in CI.
2. Have native speakers review the es, fr, de and ar strings, and check the RTL layout in a
   browser.
3. Collect more real statements (with authorisation) for the scorecard, especially
   multi-account, credit-card and non-Latin-script ones.
4. If more than one API process is needed, move `jobs.py` onto a queue and SQLite onto
   PostgreSQL.
