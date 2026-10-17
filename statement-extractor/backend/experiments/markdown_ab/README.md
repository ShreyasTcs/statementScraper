# Markdown A/B PoC

A self-contained experiment that compares two ways of feeding a bank statement to a **local** language model, with the
existing deterministic engine as a reference:

| Column | What runs |
|---|---|
| **A · Direct OCR/Text + Local LLM** | The application's own `analyze(engine="groq")` with a local Ollama client injected. The model reads the existing numbered layout lines (`document_lines`). |
| **B · Markdown + Local LLM** | The same reading result converted to Markdown (one section per page, transaction tables as Markdown tables, everything else verbatim). Its numbered lines go through the same extractor, schema, result mapper, cited-value verification, validation and completeness. |
| **Reference · Deterministic local engine** | The application's own `analyze(engine="local")`. No LLM. |

Nothing in the application imports this package, and no application file was changed to build it.

## Upload UI (recommended)

From `statement-extractor/backend`, with Ollama running locally:

```bash
.venv/Scripts/python -m experiments.markdown_ab.web
```

Open <http://127.0.0.1:8765>. Then:

1. Choose a PDF, PNG or JPG.
2. Pick a local model (only models whose digest was verified are offered).
3. Choose **Full comparison**, which runs A, B and the reference, or **Markdown only**, which runs no model.
4. Press **Upload and run**.

The progress page refreshes every 5 seconds and opens the side-by-side report when the run is finished.

- **Queue:** uploads are processed one at a time.
- **Local only:** the server listens on 127.0.0.1 only, and every upload repeats all local-only checks.
- **Storage:** the file is stored only in its run folder.
- **Ground truth:** an upload that is byte-identical to a statement in `data/samples` or `data/corpus` is scored against
  that statement's ground truth. Any other upload is compared without a scorecard.
- **Duration:** a full comparison of a one-page statement takes about 25 minutes on a 2-core CPU, because each
  language-model pass writes a few thousand tokens.
- **Options:** `--port` changes the port.
- **Groq (external, optional):** when `backend/.env` holds a Groq key, the upload page also offers
  `openai/gpt-oss-20b` on Groq. The statement text is then sent to Groq, so every Groq upload needs the consent tick,
  and the server refuses it without one. The in-process socket guard allows only Groq's API addresses. On the command
  line the same option is `--provider groq`.
- **Table grid section:** every comparison report ends with a table-grid section. It shows how many amounts the grid
  moved to another column for the reference engine, and for A and B the result of replaying the recorded model answers
  with the grid check. It makes no new model calls.

## Command line

From `statement-extractor/backend` (the application's virtual environment; Ollama running locally):

```bash
# list the statements you may choose from
.venv/Scripts/python -m experiments.markdown_ab.run --list

# rebuild data/evaluations/index.html (every run and batch, with links; also rebuilt after every run)
.venv/Scripts/python -m experiments.markdown_ab.run --index

# one statement (only data/samples and data/corpus are accepted)
.venv/Scripts/python -m experiments.markdown_ab.run --statement ../data/corpus/uk_split_balance_bfwd.pdf --model llama3.2:3b --pin a80c4f17acd5

# a set: mocks (data/samples), corpus (data/corpus) or selection (the two model-selection statements)
.venv/Scripts/python -m experiments.markdown_ab.run --set mocks  --model llama3.2:3b --pin a80c4f17acd5
.venv/Scripts/python -m experiments.markdown_ab.run --set corpus --model llama3.2:3b --pin a80c4f17acd5

# inspect the Markdown only (no model calls)
.venv/Scripts/python -m experiments.markdown_ab.run --set corpus --markdown-only
```

`--pin` is the expected model digest (at least its first 12 hex digits). `ollama list` shows the installed digests. The
command prints each run folder and report path, and for a set also the batch summary path. Open `report.html` or
`summary.html` directly in a browser. They are single files that load nothing from the network.

Model calls run one after another (read once → A → B → reference), so a corpus statement takes several minutes on a
CPU-only machine.

## What a run writes

`data/evaluations/<run-id>/` (a new folder per run; nothing is overwritten):

| File | Content |
|---|---|
| `report.html` | The side-by-side comparison: run summary and local-only checks, intermediate representations, three-column results, discrepancy explorer, ground-truth scorecard |
| `run.json` | Run metadata, statement characteristics, model identity, guard results, timings, Markdown stats and warnings, scores |
| `input/direct_lines.txt` | Pipeline A's input (the existing layout lines) |
| `input/markdown.md`, `input/markdown_lines.txt` | Pipeline B's Markdown and the numbered lines sent to the model |
| `input/markdown_provenance.json`, `input/markdown_warnings.json` | Page, table row and original line(s) of every Markdown line; conversion warnings |
| `pipelines/{A,B,reference}/result.json` | The validated result (fields, totals, rows, checks, completeness) or the failure |
| `pipelines/{A,B}/llm_outcomes.json`, `llm_calls.json` | Chunk outcomes with the parsed structured output; every raw model answer with timing and token counts |
| `score.json`, `discrepancies.json`, `network.json` | Ground-truth metrics, cross-pipeline alignment, local-only verification |
| `manifest.json` | Every file of the run with size and SHA-256 |

A set also writes `data/evaluations/batch-<id>/summary.html` and `batch.json`. Runs, pipeline outcomes and metrics are
inserted, never updated, in `data/evaluation.db`. That file is separate from the live `statements.db`.

## Safety boundaries

- **Inputs:** only `data/samples` and `data/corpus` are accepted. `companySamples` (confidential), `uploads` and `fuzz`
  are refused before any file is read.
- **Application settings in this process:** importing the package sets external LLM calls off, an empty Groq key, the
  local engine, and a database URL inside a folder that never exists. The run then checks:
  - that a Groq client cannot be built;
  - that the database setting is that unused path, and that it still does not exist afterwards.
- **Endpoint:** only `http://127.0.0.1`, `::1` or `localhost` resolving to loopback is accepted.
- **Model:** the installed digest must match `--pin`, and the model must not be cloud-hosted. Every answer must report
  the requested model.
- **Socket guard:** inside the process, any connection to a non-loopback address is refused.
- **File guard:** a Python audit hook blocks any attempt by this process to open the live database or its WAL/SHM files,
  the database sentinel, or anything in `companySamples` or `uploads`.
- **Network monitor:** `netstat` is sampled every 2 s during a run. A non-loopback connection from this process or the
  Ollama server stops the run and marks it failed. Connections of the Ollama tray app, for example update checks, are
  recorded as background. Sampling can miss connections shorter than the interval; the socket guard does not depend on
  sampling.
- **Logs:** nothing prints statement text, prompts or answers. Those stay in the run folder.

## Scoring rules (fixed before any run; `score.py`)

- **Transactions:** one-to-one match on date + debit/credit direction + exact amount (no tolerance). Each labelled row
  is used at most once. Precision, recall and F1 are reported with missing and extra counts.
- **Field-level metrics:**
  - amount + direction, ignoring the date;
  - amount only, where the gap to the line above counts direction errors;
  - date, on the rows matched by amount + direction.
- **Running balance:** scored only on strictly matched rows whose ground truth has a balance.
- **Balances:** opening and closing balance compared exactly.
- **Statement fields:** scored only where the ground truth asserts them, including fields asserted absent.
- **Description:** **not measurable**. The existing ground truth has no description labels.
- **Duplicates:** extracted rows whose key occurs more often than in the ground truth.
- **Coverage:** every metric shows eligible, labelled, correct, incorrect and unscored counts. There is no composite score.

## Known differences between A and B

- **Prompt:** both use the application's prompt unchanged. It describes horizontally aligned lines, which fits A's input
  better than Markdown.
- **Schema:** the Ollama client shows the JSON schema to the model in the system message, as Groq's API does for the
  hosted model. The text is identical for A and B.
- **Columns:** B's columns come from the existing table detector, so B inherits its row and column decisions. Columns
  are in a fixed order (date, value date, description, reference, debit, credit, amount, Dr/Cr, balance, other), with
  the printed heading where the detector recorded one. Otherwise the detector's role name is used, with a warning.
- **Provenance:** B's provenance is per table row, because the detector does not keep cell coordinates.
- **Checking B:** a token check confirms that every printed token of every direct line appears in B's Markdown for that
  page. It does not prove that the detector put each token in the right column.

## Limitations

- **Evidence base:** synthetic statements only. These show the mechanism works, not real-world accuracy. Confidential
  statements are refused by design.
- **Model scope:** results hold for one local model at temperature 0 with a fixed seed, on one machine.
- **Unchanged-app check:** the existing application files are checked by hash before and after. That proves the files
  were not changed, not full runtime compatibility.

## Remove it

Delete `backend/experiments/` and, if wanted, `data/evaluations/` and `data/evaluation.db`. The application does not
depend on any of them.
