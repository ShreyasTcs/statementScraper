# Deployment guide

This guide is for running Statement Extractor for people other than its developer, on a
team server or an internal VM. The defaults are for a single developer on one machine.
Read **Before you expose it** first.

## 1. Before you expose it

| Default | Why it is unsafe on a shared server | Set instead |
|---|---|---|
| `AUTH_MODE=none` | Anyone who can reach the API can read and delete every statement. | `AUTH_MODE=token` and `API_TOKENS=user:<token>,…` (tokens of 16+ random characters, one per person) |
| HTTP on port 8000 | Statements and tokens travel in clear text. | A TLS-terminating reverse proxy (section 4) |
| `CORS_ORIGINS` = localhost | Only matters for the browser UI. | The exact origin the UI is served from |
| `RETENTION_DAYS=0` | Statements are kept forever. | Whatever your data policy requires |

The backend logs a warning at start while `AUTH_MODE=none`. If `AUTH_MODE=token` has no valid
tokens it logs `auth_tokens_missing` and every request is refused.

**External processing.** The `groq` and `hybrid` engines send statement text to Groq. Turn
them on (`ALLOW_EXTERNAL_LLM_CALLS=true` plus `GROQ_API_KEY`) only if your data-protection
rules allow customer bank data to be processed by Groq. Confirm Groq's data-retention terms
for your account first. The UI asks each user for consent before an external upload, but the
server setting is the real control.

## 2. Requirements

- Python 3.11+ (developed on 3.13) and Node 20+ (to build the UI).
- About 1 GB of RAM per worker. OCR (RapidOCR, ONNX) runs on CPU at about 5-8 s per scanned
  page.
- Disk for uploads plus the SQLite database. Keep both on the same encrypted volume.

## 3. Install

```bash
git clone <repo> statement-extractor && cd statement-extractor/backend
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env              # then edit .env
cd ../frontend && npm ci && npm run build            # static files in frontend/dist
```

Minimum `.env` for a shared server:

```
AUTH_MODE=token
API_TOKENS=alice:<random-32-chars>,bob:<random-32-chars>
CORS_ORIGINS=["https://statements.example.internal"]
RETENTION_DAYS=90
DATABASE_URL=sqlite:////srv/statement-extractor/data/statements.db
UPLOAD_DIR=/srv/statement-extractor/data/uploads
# optional external engine:
# EXTRACTION_ENGINE=hybrid
# ALLOW_EXTERNAL_LLM_CALLS=true
# GROQ_API_KEY=<from the Groq console>
```

Never put the key on the command line or in a committed file. Use `.env` (readable only by
the service account) or your platform's secret store as environment variables.

## 4. Run

**Backend:** one process.

```bash
cd backend && .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
```

Run exactly **one** API process (no `--workers N`, no several replicas). Background jobs live
in an in-process thread pool (`PROCESSING_WORKERS`, default 2), and any run left half-done at
start-up is marked `interrupted`. A second process would mark the first one's live runs as
interrupted. To scale out, replace `app/services/jobs.py` with a real queue (Celery, RQ or
Arq), keeping the same `execute(run_id)` contract, and use PostgreSQL instead of SQLite.

**Reverse proxy** (nginx example): serve the built UI and pass `/api` to the backend.

```nginx
server {
    listen 443 ssl;
    server_name statements.example.internal;
    # ssl_certificate ...; ssl_certificate_key ...;
    client_max_body_size 26m;              # a little above MAX_FILE_SIZE_BYTES
    root /srv/statement-extractor/frontend/dist;
    location / { try_files $uri /index.html; }
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;          # uploads return at once; processing is in the background
    }
}
```

**Service** (systemd example):

```ini
[Unit]
Description=Statement Extractor API
After=network.target

[Service]
User=stmt
WorkingDirectory=/srv/statement-extractor/backend
EnvironmentFile=/srv/statement-extractor/backend/.env
ExecStart=/srv/statement-extractor/backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## 5. Database and upgrades

- On start the backend brings the database to the current schema version. Changes are
  additive only. Before any change, the file is copied with SQLite's online backup API to
  `statements.db.bak-v<from>-<UTC time>` (`DB_BACKUP_BEFORE_MIGRATION=true`).
- **Upgrading:** stop the service, deploy the new code, start it, and check the log for
  `database migrated to schema version N`. Keep the `.bak` file until you have checked the
  result.
- **Backups while running:** use `sqlite3 statements.db ".backup backup.db"` or the Python
  `sqlite3` backup API. A plain file copy can miss pages that are still in `statements.db-wal`.
- **Rollback:** stop the service and restore the `.bak` file. Newer columns are simply absent
  from the old schema; nothing else needs undoing.

## 6. Operations

| Need | How |
|---|---|
| Liveness | `GET /api/v1/health`: no auth, no secrets |
| What is configured | `GET /api/v1/config`: engines, why they are unavailable, limits |
| A stuck document | It cannot stay stuck: failures are recorded on the run, and runs left by a restart become `interrupted`. Users can reprocess. |
| Delete one statement | `DELETE /api/v1/documents/{id}` removes the rows, the corrections and the file |
| Retention | `RETENTION_DAYS`: purge at start and every 6 hours |
| Logs | Ids, counts, durations and codes only. Statement text, prompts and model answers are never logged. Exception traces go to the server log only. |
| Groq usage | Each run stores `llm_calls`, `llm_input_tokens`, `llm_output_tokens`, `llm_latency_ms` and per-chunk outcomes (`GET …/status`) |

## 7. Verifying an installation

```bash
cd backend && .venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests          # no network, no key
curl -s https://statements.example.internal/api/v1/health
```

To check Groq on a server where it is enabled, use only a synthetic statement:

```bash
RUN_LIVE_GROQ=1 LIVE_GROQ_API_KEY=<key> .venv/bin/python -m pytest tests/test_groq_live.py
```

Do not evaluate with real customer statements unless you are authorised to send them to Groq.
`tests/evaluate_engines.py` refuses the `companySamples` folder for live runs without an
explicit flag.
