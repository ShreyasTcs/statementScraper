# Statement Extractor

Reads a bank statement (a text PDF, a scanned PDF or a photo) from any bank, country,
language or currency. It extracts the account details and every transaction, checks them
against the statement's own arithmetic, and gives a **completeness verdict** that is never
assumed: `complete`, `partial`, `needs_review` or `failed`. A reviewer can then correct
anything. Every change is audited and the original values are kept.

- **Three engines.** `local` is deterministic and runs entirely on this machine (the default).
  `groq` uses Groq `openai/gpt-oss-20b` with schema-constrained output. `hybrid` runs both and
  keeps whichever result the arithmetic proves. External engines are off unless they are
  explicitly enabled.
- **Proof, not trust.** Checks cover the running balance on every row, opening + credits −
  debits = closing, printed totals, the period, date order and duplicate rows. Newest-first
  statements are detected and checked in chronological order.
- **Evidence for every value.** Each value records its page, bounding box and source lines.
  Language-model values must be found again in the lines they cite, or they are flagged.
- **Nothing silently dropped.** A line that looks like a transaction but did not become a row
  is listed as "not extracted". An amount whose direction is unknown is kept as an amount, not
  guessed. A suspected duplicate is flagged, not removed.
- **Review workflow.** You can edit fields and transactions, add, delete or restore rows,
  confirm values, mark duplicates as genuine and dismiss lines. Every edit is revalidated
  immediately. The full audit trail is kept, along with the frozen originals.
- **Multilingual UI.** English, Spanish, French, German and Arabic (right-to-left).
  Formatting follows the locale, and money is formatted as exact decimal strings, never
  floats. Text copied from the statement is never translated.

## Quick start

```bash
# backend
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt          # Windows
# source .venv/bin/activate && pip install -r requirements.txt     # macOS / Linux
cp .env.example .env                                              # optional: defaults are safe
.venv/Scripts/python -m uvicorn app.main:app --port 8000

# frontend (second terminal)
cd frontend
npm install
npm run dev                # http://localhost:5173 (proxies /api to port 8000)
```

On start, the backend creates the database or upgrades it in place, with a backup first
(`data/statements.db.bak-v<from>-<time>`). See [Database](#database). Check what is active:
`curl http://127.0.0.1:8000/api/v1/health`.

## Engines

| Engine | What happens | Sends data out? |
|---|---|---|
| `local` (default) | Geometry + multilingual vocabulary + deterministic parsing. Details in `docs/research.md`. | No |
| `groq` | The document becomes numbered, layout-preserving lines. `openai/gpt-oss-20b` returns, per chunk, *where* each value is (strict JSON schema, temperature 0, fixed seed). Every value is then parsed with the document's own number and date conventions and **looked up again in the cited lines**. | Yes: statement text to Groq |
| `hybrid` | Runs both. Keeps the result that reconciles (ties go to `local`), records every disagreement, fills failed chunks with local rows (marked as such), and falls back to `local` if Groq fails, **saying so** (`engine_used=local`, `fallback_reason`). | Yes |

The `groq` engine **never falls back silently**. If Groq fails, the run fails with a stable
error code (`llm_auth`, `llm_rate_limited`, `llm_timeout` ...). A response that reports a
model other than `openai/gpt-oss-20b` is rejected (`llm_model_mismatch`).

To enable Groq, set all three of these (see `backend/.env.example`):

```
GROQ_API_KEY=<your key>
ALLOW_EXTERNAL_LLM_CALLS=true
EXTRACTION_ENGINE=groq        # or hybrid; or leave local and choose per upload
```

Without them, uploads that ask for `groq` / `hybrid` get `422 external_calls_disabled` /
`groq_key_missing`, and the UI shows those engines as unavailable with the reason. The UI
also asks for explicit consent before uploading with an external engine.

What the language-model layer does, all in `backend/app/services/llm_extraction_service.py`:

- **Input (`GROQ_INPUT`).** By default (`markdown`, prompt 2026-10.3) the model reads the
  statement as Markdown (`backend/app/services/markdown_view.py`). The transaction tables come
  from the local table detector with one cell per column, so an empty debit or credit cell stays
  empty. Recognised balance and totals lines are kept outside the table, and all other text is
  kept verbatim. The model also gets the layout reader's notes: the number format, what each
  column heading means, and which lines are balances. If the Markdown would lose any printed
  token, the run falls back to `lines`, the layout lines with their spacing (prompt 2026-10.2).
- **Balance lines are not transactions.** A row the model reports as a transaction is dropped if
  either of these holds:
  - the model also reported that line, with the same amount, as a balance line;
  - the row leads with an opening, closing or carried-forward label, judged with the local
    detector's own rule.

  Such a row is kept as a balance row, and the result notes how many were moved.
- **Printed signs win.** If the model puts an amount on the wrong side, the printed sign or Dr/Cr
  marker decides the side (`2.156,31+` is a credit). A period that ends before it starts is put
  back in order, which happens with right-to-left text.
- **Currency.** If the model does not cite a printed currency, the local engine's evidence fills
  the gap or confirms the model's value: printed codes and symbols, and the IBAN's country. That
  evidence never replaces a currency the model gave, so a USD account at an Italian bank stays USD.
- **Chunking.** Pages are packed under an input-token budget (`GROQ_CHUNK_MAX_INPUT_TOKENS`,
  default 3500, sized for the free tier's ~8K tokens per minute). A very long page is split
  by lines. Each chunk carries the previous chunk's last lines as read-only context, and
  accounts get stable ids (A1, A2 ...) across chunks.
- **Calls and retries.** A malformed answer gets one repair attempt. When Groq rejects the
  format, the repair note names the place and rule the validator reported (for example an
  invented field name), never the answer itself. A truncated answer
  (`finish_reason=length`) splits the chunk and retries. A rate limit is waited out within
  `GROQ_RATE_LIMIT_MAX_WAIT_SECONDS`. Authentication and model errors stop the run.
  Per chunk, only tokens, latency and error codes are stored, never content.
- **Prompt-injection defence.** Statement text is data inside a delimited block that the
  text itself cannot close. The system prompt says the text is never instructions. Most
  importantly, nothing the model says is trusted until it is verified in the document.
- **Omission check.** Any processed line holding a date and an amount that the model did
  not cite is reported as "not extracted".

## Completeness

| Status | Meaning |
|---|---|
| `complete` | Every page was processed and the arithmetic proves the rows: the running balance reconciles, or opening + credits − debits = closing, or the printed totals match. Also needed: no failed check, nothing unresolved or unverified, and the required details present. A statement that prints "no transactions" is complete when its balances agree. |
| `partial` | A page could not be read or a language-model chunk failed: data may be missing. |
| `needs_review` | Everything was processed, but something is unproven or suspicious: the books do not balance, duplicates, lines not extracted, unverified values, unknown directions, missing required details, or nothing to prove the rows against. |
| `failed` | Nothing usable (unreadable file, engine failure). |

Each verdict lists its reasons as message codes (translated in the UI).

## Multiple accounts and currencies

The `groq` engine reports each account, and each currency of a multi-currency account, as a
separate ledger. Each ledger is validated on its own; currencies are never added together.
The local engine reads a file as one account. It flags documents that look like several
(`multiple_accounts_suspected`) instead of guessing.

## Review and audit

The result page lets you:

- edit any detail;
- edit, add, delete or restore any transaction;
- confirm a row or detail you have checked against the original;
- mark a flagged duplicate as genuine;
- add a "not extracted" line as a transaction, or dismiss it.

Every action calls the API, writes a `corrections` record (who, when, attribute, old → new)
and revalidates at once, so checks and completeness are never stale. Extracted values are
frozen in `transactions.original` / `extraction_fields.original_value`. Deletion is soft.
Exports (CSV, JSON) reflect the corrections and leave out deleted rows. CSV cells that a
spreadsheet would run as formulas are neutralised.

Input is normalised with the statement's own conventions, so on a German statement you can
type `1.234,56`. ISO dates and plain decimals always work.

## API

All routes are under `/api/v1`. Errors are `{"detail": {"code", "message", "params"}}` with
stable codes. A document owned by another user returns 404.

| Method | Path | Purpose |
|---|---|---|
| POST | `/documents` | Upload (`file`, optional `engine`). Returns 202 and processes in the background (`?wait=true` processes before answering). The same file still processing for the same user is not processed twice. |
| GET | `/documents` | History: `limit`, `offset`, `q` (filename) |
| GET | `/documents/{id}/status` | Live run state: stage, progress, engine, model, tokens, per-chunk outcome, error code |
| GET | `/documents/{id}/result` | Accounts, details, transactions, checks, totals, completeness, engine, evidence |
| PATCH | `/documents/{id}/fields/{field_id}` | `{"value": ...}` or `{"confirm": true}` |
| PATCH | `/documents/{id}/transactions/{txn_id}` | Any of date, value_date, description, reference, debit, credit, amount, balance, currency; `confirmed`; `not_duplicate` |
| POST | `/documents/{id}/transactions` | Add a row (`after_index`, `account`, values) |
| DELETE | `/documents/{id}/transactions/{txn_id}` | Soft delete (`POST …/restore` to undo) |
| POST | `/documents/{id}/unresolved/{account}/{index}/dismiss` | Dismiss (`?dismissed=false` to undo) |
| POST | `/documents/{id}/revalidate` | Re-run all checks on the stored, corrected data |
| POST | `/documents/{id}/reprocess` | New run with `{"engine": ...}`; history keeps the old one |
| GET | `/documents/{id}/corrections` | Audit trail |
| GET | `/documents/{id}/transactions.csv`, `/result.json` | Exports |
| GET | `/documents/{id}/preview` | The original file |
| DELETE | `/documents/{id}` | Delete the document, its data and its file |
| GET | `/config` | Engines and why unavailable, limits, auth mode |
| GET | `/health` | Liveness, schema version, engines (no secrets) |

## Security

- **Authentication.** `AUTH_MODE=none` (the default) is one local user and is for a trusted
  machine only. `AUTH_MODE=token` with `API_TOKENS=alice:<token>,bob:<token>` requires
  `Authorization: Bearer` on every route except health. Each user sees only their own
  documents.
- **Limits.** Upload size (`MAX_FILE_SIZE_BYTES`, read bounded), page count (`MAX_PAGES`), and
  file type checked by its bytes. Password-protected PDFs are refused.
- **Privacy.** No external call unless explicitly enabled. Logs carry ids, counts and codes,
  never statement content, prompts or responses. Clients never see stack traces. Validation
  errors name fields without echoing values. API responses are `Cache-Control: no-store`.
- **Retention.** `RETENTION_DAYS` deletes documents, data and files automatically.
  `DELETE /documents/{id}` deletes on demand.
- **CORS.** Explicit origins only (`CORS_ORIGINS`).

## Database

SQLite (WAL mode). Schema changes are **additive, versioned migrations**
(`backend/app/db/migrations.py`). Columns and tables are only ever added. Before any pending
migration, the file is copied with SQLite's online backup API. Each migration runs in a
transaction and is idempotent. A database never needs to be deleted to upgrade. Results
extracted by an earlier version show `needs_review` ("not assessed") until you click
**Check again**.

## Tests

```bash
cd backend
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest tests                      # 62 tests (+1 live test, skipped): no network, no API key
.venv/Scripts/python tests/test_pipeline.py              # scorecard on sample folders (see below)
.venv/Scripts/python tests/fuzz.py 1000 2026             # 1,000 random statements
.venv/Scripts/python tests/evaluate_engines.py --engines local,groq,hybrid --client simulated

cd ../frontend
npm test                                                  # 30 tests: i18n parity, formatting, components
npm run build
```

The Groq tests use **scripted answers from a simulated model**. They are not recorded Groq
responses, because no API key was available while this was built. They test the
deterministic layer around the model: verification, chunking, retries, fallback, mapping
and completeness. A live smoke test (`tests/test_groq_live.py`) runs only with
`RUN_LIVE_GROQ=1` and `LIVE_GROQ_API_KEY`, and only on a synthetic statement.

`tests/evaluate_engines.py` compares engines on statements with ground truth. It reports
header accuracy, transaction precision and recall, direction accuracy, duplicated and
omitted rows, reconciliation rate, latency, tokens, and cost when
`GROQ_PRICE_INPUT_PER_MTOK` / `GROQ_PRICE_OUTPUT_PER_MTOK` are set. It refuses to send the
confidential sample folder to Groq without an explicit flag.

Sample folders for the scorecard:

- `data/samples`, generated by `tests/make_samples.py`;
- `data/corpus`, generated by `tests/corpus.py`;
- `data/companySamples`, real statements with hand-written `.expected.json`. This folder is
  git-ignored and must never be shared.

## Evidence (local engine)

| Set | Statements | Result |
|---|---|---|
| Real company statements (three banks, three countries; two are scans) | 7 | all correct |
| Original mocks | 3 | all correct |
| Synthetic corpus: 14 conventions, 7 scripts, scans in four orientations | 28 | all correct |
| Random combinations (`tests/fuzz.py`) | 1,000 | all correct |

The synthetic sets were written by the same hand as the extractor. They show that the
conventions that were thought of are handled, not that every bank is. Only the seven real
files are independent of that.

## Known limits

- **Groq has been measured on synthetic statements only** (7 scenarios: row recall 0.975,
  4 of 7 complete; `docs/IMPLEMENTATION_REPORT.md`, section 5). In tables without a running
  balance it can put credits in the debit column. Reconciliation catches this, and `hybrid`
  corrects it. Measure on real layouts with `tests/evaluate_engines.py --client live`, using
  authorised documents only.
- The local engine treats a file as one account. Credit-card and investment statements are
  not specifically supported.
- OCR's bundled models read Latin script and Chinese. Other scripts need other OCR models.
  Curled or perspective-distorted photos are not dewarped.
- Processing is a single-process thread pool. To run several API processes, move jobs to a
  real queue (`docs/DEPLOYMENT.md`).

More detail: `docs/IMPLEMENTATION_REPORT.md` (what was built and verified),
`docs/DEPLOYMENT.md` (running it for others), `docs/research.md` (design research).
