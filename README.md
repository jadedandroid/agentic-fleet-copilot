# Agentic Fleet Copilot

Deterministic, multi-tenant full-stack reference implementation that turns static IT telemetry snapshots into:

- evidence-grounded operational answers
- deterministic anomaly insights
- auditable, human-approved remediation actions

## Stack

- **Backend orchestration:** Python, LangGraph, FastAPI, Pydantic
- **Data and guardrails:** DuckDB (time-series snapshots), SQLite + append-only JSONL audit log
- **Frontend:** Next.js (App Router), TypeScript, Tailwind CSS
- **Evaluation:** Pytest backend suite

## Prerequisites

- Python 3.11+ (tested with 3.14)
- Node.js 18.17+ (required by Next.js 14)

## Architecture highlights

1. **Deterministic orchestration:** LangGraph state graph with fixed stages (`load_context -> detect_anomalies -> compose_answer -> approval_gate?`).
2. **Tenant isolation:** Every data read/write path requires and scopes by `tenant_id`.
3. **Evidence grounding:** Responses include citations to concrete snapshot IDs and timestamps.
4. **HITL guardrail:** Any proposed remediation is recorded as `pending` until explicitly approved/rejected.
5. **Auditability:** Every key event is persisted both to SQLite and append-only JSONL.
6. **Evidence threshold guardrail:** State-changing actions are refused when evidence quality is insufficient.

> Runtime note: when DuckDB wheels are unavailable for the active Python version, telemetry storage transparently falls back to SQLite while keeping the same multi-tenant query behavior.

## Run backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Run frontend

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

The frontend expects backend at `http://localhost:8000`.

Pages:

- `/` Home: links to each section.
- `/about` What the system does, its guarantees, and its known limits.
- `/agent-flow` The eight-stage pipeline with guardrails, plus a live run panel that shows each stage and guardrail check for one question.
- `/copilot` Grounded Q&A, insights, citations, and the approval gate for proposed actions.
- `/source` A simulated telemetry feed that pushes synthetic snapshots into the tenant fleet. Only synthetic data is generated.

The tenant API key is entered once and shared across pages through `sessionStorage`.

For local CORS, backend accepts both `http://localhost:3000` and `http://127.0.0.1:3000` by default. Override with `AFC_CORS_ORIGINS` (comma-separated).

## Usage

### Credentials and environment variables

Set these on the backend. Use placeholders in shared files; never commit real values.

| Variable | Purpose | Required |
|---|---|---|
| `AFC_BOOTSTRAP_ADMIN_KEY` | Platform admin key for creating and deleting tenants | To manage tenants |
| `AFC_JUDGE_KEY_SECRET` | Fernet key that encrypts saved OpenAI keys | To save keys (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) |
| `AFC_RAGAS_ENABLED` | `1` enables the grounding judge and the LLM planner on the server | Only for judging and planning |
| `OPENAI_API_KEY` | Server-side OpenAI key for the judge and planner | Only with `AFC_RAGAS_ENABLED=1` |
| `AFC_API_KEYS` | Service keys as `key=tenant` pairs | Optional |
| `AFC_SEED_DEMO` | `true` seeds the demo tenants | Optional |
| `AFC_DATASET_ADMIN_PASSWORD` | Password for the dataset loader's tenant admins (12+ characters) | Only when loading the dataset |

An OpenAI key is required only for the grounding judge and the LLM planner. Every other feature works without one.

### Sign in and ask a question

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "content-type: application/json" \
  -d '{"email":"<your-admin-email>","password":"<your-password>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -s -X POST http://127.0.0.1:8000/api/v1/copilot/query \
  -H "authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"question":"Which devices are NOT low on disk space?","allow_remediation":false}'
```

Questions that span the dataset's history need a reference time, for example `"as_of":"2026-06-12T12:00:00Z","context_window_minutes":43200`.

### Upload telemetry

`POST /api/v1/fleet/uploads?filename=<name>` takes the file as the raw body, with a `Content-Type` that matches it. The whole file is checked before anything is stored: any bad record rejects the upload and returns the row or line number.

| Format | Content-Type | Accepted shape |
|---|---|---|
| JSON | `application/json` | A fleet snapshot (`{"source","observed_at","devices":[...]}`), a list of them, or agent records (the take-home format) as an array |
| NDJSON | `application/x-ndjson` | One agent record or fleet snapshot per line |
| CSV | `text/csv` | One device per row, grouped by `observed_at`. Required columns: `observed_at, device_id, employee_id, device_type, os_name, os_version, disk_free_pct, ram_used_pct` |

An agent record needs `device_id`, `company_id`, `employee_id`, `collected_at`, `os`, `device_identity` (with a known `model_name`), `memory`, `disk_volumes`, and `compliance_results`. Each upload must hold one `company_id`. Limits: 5 MB, 500 devices per snapshot, 10,000 rows.

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/fleet/uploads?filename=fleet.ndjson" \
  -H "authorization: Bearer $TOKEN" -H "content-type: application/x-ndjson" \
  --data-binary @device-telemetry-dataset.ndjson
```

### Grounding judge with your own OpenAI key

The judge is off by default. Enter a key in the copilot page's judge panel, or call the API:

```bash
# Save a key for 7, 30, or 90 days (encrypted; needs AFC_JUDGE_KEY_SECRET on the server)
curl -s -X PUT http://127.0.0.1:8000/api/v1/judge-key -H "authorization: Bearer $TOKEN" \
  -H "content-type: application/json" -d '{"openai_api_key":"sk-<your-key>","ttl_days":30}'

# Run the judge with the saved key, or pass "openai_api_key" for a one-time run
curl -s -X POST http://127.0.0.1:8000/api/v1/evals/grounding-judge -H "authorization: Bearer $TOKEN" \
  -H "content-type: application/json" -d '{"question":"Which devices at Acme are low on disk space?"}'

# Remove the saved key
curl -s -X DELETE http://127.0.0.1:8000/api/v1/judge-key -H "authorization: Bearer $TOKEN"
```

A rejected key returns `502` with the error type only. The key is never returned, logged, or audited.

### Run the evaluations

```bash
cd backend
pytest -q                                   # deterministic suites, no network
AFC_RAGAS_ENABLED=1 OPENAI_API_KEY=sk-... pytest -m ragas -q   # live judge, writes evals/reports/
```

## Authentication and tenants

Users sign in with email and password (`POST /api/v1/auth/login`). The server returns a session token (8 hours by default, `AFC_SESSION_TTL_HOURS`) and the tenant and role come from the account, never from the client. Passwords are hashed with scrypt (standard library). Session tokens are random, and only their SHA-256 hash is stored.

- **Roles:** `admin` can approve or reject actions and manage users. `analyst` can ask questions and upload data but cannot approve.
- **Platform admin:** creates and deletes tenants. Requires the bootstrap key in the `x-bootstrap-key` header, set through `AFC_BOOTSTRAP_ADMIN_KEY`. If the variable is unset, these endpoints return 403.
- **Service keys:** `AFC_API_KEYS` (`key=tenant` pairs) remain for scripts and the seeded demo tenants. Each key is a full admin of its tenant. Demo keys are `acme-demo-key` and `globex-demo-key`.

### Tenant lifecycle

1. The platform admin creates a tenant with a name, an admin email, and a password of at least 12 characters. The tenant ID is derived from the name (`Initech Corp` becomes `initech-corp`). IDs are never reused.
2. The tenant admin signs in and adds users with `POST /api/v1/tenant/users`.
3. Deleting a tenant (`DELETE /api/v1/admin/tenants/{tenant_id}`) runs in one transaction. It removes that tenant's devices, telemetry, compliance results, users, and sessions, and marks the tenant `deleted`. Its service keys and tokens stop working immediately.
4. The audit log is append-only, so tenant audit rows are kept. The platform admin can read them at `GET /api/v1/admin/tenants/{tenant_id}/audit`. Tenant users cannot.

Emails are unique across the platform, because login takes only an email and a password.

## API overview

Authenticated routes accept `Authorization: Bearer <session token>` or `x-api-key: <service key>`.

- `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`
- `POST /api/v1/fleet/snapshots` ingests a fleet snapshot as JSON (strict Pydantic schema)
- `POST /api/v1/fleet/uploads?filename=...` ingests a JSON or CSV file as a raw body with a matching `Content-Type`. The whole file is validated first. Any error returns 422 with row numbers and stores nothing. Limits: 5 MB, 500 devices per snapshot, 10,000 rows.
- `GET /api/v1/fleet/devices` lists the caller's devices
- `POST /api/v1/copilot/query` runs a grounded query and returns answer, traces, citations, insights, and any proposed action. The optional `as_of` field pins the reference time.
- `GET /api/v1/copilot/stream?apiKey=...&question=...` streams SSE events (`start`, `trace`, `insights`, `proposed_action`, `interrupt`, `final`). `apiKey` accepts a session token or a service key, and the optional `asOf` parameter pins the reference time. It is a query parameter because `EventSource` cannot set headers, so the credential appears in URLs and logs. This is a documented trade-off.
- `POST /api/v1/copilot/approvals/{action_id}` records an approve or reject decision (admin only) and resumes the LangGraph thread. A signed-in reviewer is recorded by email.
- `POST /api/v1/copilot/actions/{action_id}/execute` runs an approved action once (idempotent; requires prior approval)
- `GET /api/v1/audit/events` returns `{"events": [...]}` for the caller's tenant
- `GET|POST /api/v1/tenant/users` lists or adds users in the caller's tenant (admin only)
- `GET|POST /api/v1/admin/tenants`, `DELETE /api/v1/admin/tenants/{tenant_id}`, `GET /api/v1/admin/tenants/{tenant_id}/audit` (bootstrap key)

Pending approvals are checkpointed to `data/checkpoints.sqlite3` (LangGraph `SqliteSaver`), so they survive a backend restart. Tool actions are mock stubs and do not call external systems.

## Agent architecture

The agent is an explicit LangGraph state graph, not a prompt wrapper. Each stage is a node, and the graph is compiled with a SQLite checkpointer so it can pause and resume across restarts.

1. **Route** interprets the question as one of four operations: a single analysis, a negation (the complement of one topic), a ranking (a superlative on one metric, such as "most RAM" or "top 5"), or a conjunction (several topics joined by "and", listing devices that match all of them). It corrects one-letter typos in longer words, maps common paraphrases (for example "hard drive" to disk), and refuses only what the telemetry cannot support, with a stated reason. A misspelled rival company name is still treated as cross-tenant.
    (low disk, storage trend, RAM pressure, failing compliance, compliance drift, OS age, battery end-of-life, or cross-tenant or unsupported, which are refused). A mention of another tenant's name is refused, but a caller's own company name never is.
1a. **Plan** records the steps it will take (retrieve, analyze, compose, propose, and whether remediation is allowed) in the trace before any data is read.
2. **Retrieve and analyze** loads the caller's devices, metrics, and compliance results for the window, then runs one deterministic analysis.
3. **Compose** writes the answer from the finding, with every device cited back to its snapshot.
4. **Propose** maps the finding to one catalog tool, but only when remediation is requested and the evidence score is at least 0.70.
5. **Approval gate** calls `interrupt()`. Nothing runs until a reviewer resumes the graph.
6. **Execute and audit** run the approved tool once, after checking that its target belongs to the tenant. Every step is written to the audit log.

## Grounding strategy

- Answers come only from telemetry rows. The LLM is not in the runtime path, so the same data always yields the same answer, trace, and proposal.
- Each citation names a device, metric, value, reason, and snapshot ID. Insights list their supporting device IDs.
- If there is no evidence for the tenant in the window, the agent refuses rather than guessing.
- The evidence score is computed from sample counts and persistence across snapshots. Proposals below 0.70 are refused with a visible trace.

## Guardrails

- **Tenant isolation:** the tenant comes from the server-side session or key. Every query and write is scoped by `tenant_id`. Targets are re-checked at execution time.
- **Cross-company access:** questions naming another tenant are refused before any data is read.
- **Human approval:** state-changing actions need an admin's explicit approval. Rejected actions cannot be executed or re-approved. Pending approvals survive a restart.
- **Abuse limits:** five failed sign-ins for an email lock it for 15 minutes, and twenty failures from one client address lock that address for the same window (`429` with `Retry-After`). Unknown emails are counted too, so the limit cannot be used to probe for accounts. A successful sign-in resets the email's count.
- **Evidence threshold:** weak evidence is refused, and the refusal appears in the trace.
- **Audit:** proposals, decisions, executions, logins, tenant changes, and uploads are recorded. Upload contents are not stored in the audit log.
- **Employees and company:** every citation names the employee who holds the device, and every answer names the company (tenant display name) whose fleet it describes.
- **Battery:** if battery health is not reported for a tenant, a battery question is refused with that reason. The copilot never reports "no battery is failing" when no battery data exists.

## Design decisions and trade-offs

- **Deterministic analysis over LLM planning.** Reproducibility and testability are worth more here than open-ended planning. The trade-off is that new question types need code, not prompts.
- **SQLite fallback for DuckDB.** The same schema and queries run on both. DuckDB is used when installed.
- **Static keys kept beside accounts.** Existing scripts and tests keep working. Service keys are full admins, so they should be treated as secrets.
- **Session tokens in query strings for SSE.** `EventSource` cannot send headers. A production build would use a short-lived stream ticket.
- **Raw-body uploads.** No multipart dependency. The CSV format is one device per row, grouped by `observed_at`.
- **Email as the login identifier, unique platform-wide.** This keeps login simple. The trade-off is that a tenant cannot reuse an address for another tenant.
- **Audit kept after tenant deletion.** It is append-only by design. Audit payloads still contain device IDs for a deleted tenant, and only platform admins can read them.
- **RAM wording:** "high RAM" or "memory pressure" means the current reading (at or above 90%). "Consistently", "always", "over time", or "trend" mean a sustained pattern (at least 75% of readings). The current reading is informational and never proposes an action.
- **Negation, ranking, and conjunction** are answered from the same analyses. Their results are informational and never propose an action.
- **Re-planning loop:** when a proposal's evidence score is below 0.70, the graph widens the lookback to 30 days and analyzes again, once. That is the only cycle in the graph. If the evidence is still weak, the proposal is refused with a visible trace.
- **Known gaps:** the login throttle is in memory and per process, so it does not coordinate across replicas. No RBAC beyond two roles, and mock tools only.

## Action tool catalog

- `create_upgrade_order(device_id, component, spec)`
- `open_remediation_ticket(device_id, check_id, note)`
- `flag_device_for_replacement(device_id, reason)`
- `notify_employee(employee_id, message)`

## Dataset

The take-home dataset (`device-telemetry-dataset.ndjson`, 750 agent records, 25 devices, 3 companies, 30 days from May 14 to June 12, 2026) loads through the public API:

```bash
cd backend
AFC_BOOTSTRAP_ADMIN_KEY=... AFC_DATASET_ADMIN_PASSWORD=... \
  python scripts/load_dataset.py --file ../device-telemetry-dataset.ndjson --base-url http://127.0.0.1:8000
```

- Each company becomes a tenant with its dataset ID (`acme-001`, `globex-002`, `initech-003`) and display name (`Acme`, `Globex`, `Initech`). Its admin is `admin@<company>.dataset.test`, with the password from `AFC_DATASET_ADMIN_PASSWORD`.
- Records are mapped by `app/dataset_adapter.py`. The mapping is explicit. An unknown model, an unexpected compliance status, or a missing field is reported with its line number, and nothing is loaded. RAM and disk become percentages, and compliance `pass`/`fail` becomes `passed`.
- Rerunning the loader skips tenants that already exist, so telemetry is never duplicated.
- **Known gap:** battery health is left empty. The dataset has charge percent and cycle count but no design capacity, so the copilot does not guess battery end-of-life for this data.

### Reference time

Timestamps are kept as recorded. The copilot's lookback is 14 days from now, so the dataset returns "no telemetry" at the default time. Pass a reference time (`as_of` in the API, or the "Reference time" field on the co-pilot page). Any time after 2026-06-12 11:00 UTC covers the whole dataset. The 30-day window ends at the reference time, and rows after it are ignored.

## Validation

```bash
cd backend
pytest -q
```

The default run covers 119 deterministic tests:

- `tests/test_api.py`, `tests/test_golden.py`, `tests/test_adversarial.py`: API behavior, golden regression, and guardrail adversarial cases on a seeded demo fleet.
- `tests/test_tenancy.py`: sign-in, sessions, tenant lifecycle, roles, uploads.
- `tests/test_as_of.py`: a pinned reference time is unaffected by later rows.
- `tests/test_agent_loop.py`: the plan precedes retrieval, and weak evidence triggers exactly one widening, never a loop.
- `tests/test_behavior_durability.py`: a pending approval survives a restart, and login throttling locks, unlocks, and isolates correctly.
- `tests/test_dataset_adapter.py`: the mapping, and each way it can reject a record.
- `tests/test_dataset_eval.py`: an oracle evaluation on the real dataset. Expected answers are computed directly from the NDJSON file, with the thresholds written out in the test, and the copilot is never used to grade itself. Cited values must equal the raw values. The suite is skipped if the dataset file is absent.

### LLM-assisted planner (optional, gated)

Questions the rules cannot place (no supported topic) can be planned by an LLM. It runs only when:

1. the server has the judge enabled: `AFC_RAGAS_ENABLED=1` and `OPENAI_API_KEY` set, and
2. the request asks for it: `llm_planner: true` (the copilot page checkbox, or `llmPlanner=true` on the stream).

The model's answer must match a strict schema (one of four operations, topics from the supported list, a bounded limit) and pass consistency checks. Anything else falls back to the rules and the refusal stands. Negated, superlative, cross-tenant, and multi-topic questions never reach the model, so it cannot override them. Only the question text and the topic list go to OpenAI; no telemetry is sent. The trace records whether the rules or the LLM placed the question.

### Judge keys (bring your own key)

The grounding judge is off by default. You can run it on any answer in the copilot page by entering an OpenAI key:

- **One-time:** enter a key for a single run. It is used for that request and discarded.
- **Saved:** keep the key for 7, 30, or 90 days. It is encrypted with Fernet using `AFC_JUDGE_KEY_SECRET`, tied to your account, and removed automatically at expiry. Delete it at any time from the panel or `DELETE /api/v1/judge-key`.

Keys are never returned by the API, never written to the audit log, and never written to the database in plain text. Saving requires `AFC_JUDGE_KEY_SECRET` to be set, and service keys cannot save one. Generate the secret with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Losing `AFC_JUDGE_KEY_SECRET` makes saved keys unreadable, so users must save them again.

### Optional LLM judge (RAGAS)

The judge is opt-in because it sends each answer and its cited evidence to an external model. Raw telemetry rows are not sent. Scores vary between runs.

```bash
pip install -r requirements-evals.txt
AFC_RAGAS_ENABLED=1 OPENAI_API_KEY=... pytest -m ragas
```

It writes a JSON report to `backend/evals/reports/`. The RAGAS code was written against `ragas==0.4.4` but has not been run end to end, because no OpenAI key has been provided. Without the flag and key, the test skips with a clear reason and makes no external call. The default test run never calls it.

## Rubric evidence

Each row lists what proves the area and what is still missing. The scores in the final comparison are judgment calls based on these rows, not measurements.

| Area (weight) | Evidence in this repo | Still missing |
|---|---|---|
| Agent design (25%) | LangGraph state graph with route, plan, retrieve, analyze, compose, propose, and an `interrupt()` approval gate, checkpointed to SQLite (`app/copilot.py`). One bounded re-planning cycle on weak evidence (`test_agent_loop.py`). Tool catalog (`app/actions.py`). | Planning is rule-based, not LLM-driven. The cycle runs at most once. |
| Grounding and correctness (20%) | Cited answers (`test_api.py`, `test_golden.py`). Employee on every citation and company on every answer (`test_dataset_eval.py`). Pinned reference time (`test_as_of.py`). Oracle checks against the real dataset, six of them on raw values. Battery questions refused when battery health is not reported. Negation, ranking, and multi-topic answers computed from the same analyses. | Battery end-of-life cannot be assessed on this dataset. RAGAS judge not yet run on a live key. |
| Action quality and guardrails (20%) | Tenant isolation, cross-tenant refusal, and execution-time target checks (`test_api.py`, `test_adversarial.py`). Admin-only approvals and session auth (`test_tenancy.py`). Durable approvals across restarts and login lockout (`test_behavior_durability.py`). Audit trail. Refusal when evidence or battery data is missing. | Login throttle is per email and per client address, in memory, per process. Tools are mocks. Service keys are full tenant admins. |
| Evaluation rigor (20%) | 196 deterministic tests, including a 59-case router suite. Independent oracle on the real dataset. Adversarial and golden suites. Opt-in RAGAS judge with a gate. CI runs the deterministic suites and the frontend checks on every push (`.github/workflows/ci.yml`). | RAGAS never executed on a live key. Golden expectations come from our own seeded output, so they guard against regressions but are not evidence of correctness. |
| Engineering and communication (15%) | README covering setup, API, architecture, grounding, guardrails, trade-offs, dataset loading, and evaluation. `FINISH_PLAN.md`. Dockerfiles and compose file. CI workflow. | Docker builds are unverified because Docker is not installed here. CI runs on every push and is green. The DuckDB storage path is untested, because the verified runtime is Python 3.14, where DuckDB is skipped. |
