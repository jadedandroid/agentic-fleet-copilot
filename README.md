# Agentic Fleet Copilot

Ask questions about your device fleet in plain language. Answers come only from telemetry, every claim cites its evidence, and any change to a device needs an admin's approval.

**Live frontend:** https://agentic-fleet-copilot-ui.vercel.app (needs a backend you run or host; see [Deploy](#deploy))

## What it does

- **Answers questions** such as "Which devices at Acme are low on disk space?", "Which devices are NOT low on disk space?", "top 3 devices by memory usage", and "devices low on disk and high RAM".
- **Cites evidence:** each device in an answer shows its metric, value, employee, and snapshot.
- **Proposes actions** (upgrade order, ticket, replacement, notice). Nothing runs until an admin approves it.
- **Keeps tenants separate:** each account sees only its own company's devices.
- **Refuses** questions the data can't answer, and says why.

## Quick start

Requirements: Python 3.12+ and Node.js 20+.

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
AFC_BOOTSTRAP_ADMIN_KEY=<choose-a-long-random-key> uvicorn app.main:app --port 8000

# Frontend (new terminal)
cd frontend
npm install
npm run dev        # http://localhost:3000
```

Open http://localhost:3000, then sign in (see below).

## How to use

**1. Sign in.**

*Quick demo login (local only):* start the backend with a demo password (12+ characters). It creates a `demo` tenant with an admin account and six sample devices:

```bash
AFC_DEMO_ACCOUNT_PASSWORD='<choose-a-password>' uvicorn app.main:app --port 8000
```

Sign in with email `admin@demo.local` and that password. Do not set this variable on a public server.

*Real accounts:* the platform admin creates tenants with the bootstrap key:

```bash
curl -s -X POST http://localhost:8000/api/v1/admin/tenants \
  -H "x-bootstrap-key: <your-bootstrap-key>" -H "content-type: application/json" \
  -d '{"name":"Acme","admin_email":"admin@acme.example","admin_password":"<12+ characters>"}'
```

The tenant admin signs in on `/login`. Admins can add analysts on `/admin`.

**2. Load data.** On `/source`, upload a file. Accepted formats:

| Format | What it holds |
|---|---|
| JSON | A fleet snapshot (`{"source","observed_at","devices":[...]}`) or an array of agent records |
| NDJSON | One agent record or snapshot per line |
| CSV | One device per row, grouped by `observed_at`. Columns: `observed_at, device_id, employee_id, device_type, os_name, os_version, disk_free_pct, ram_used_pct` (optional: `battery_health_pct`, `compliance_json`) |

A file is accepted only if every record is valid. One bad row rejects the whole file and reports its row or line. Limits: 5 MB per file, 10,000 rows.

**3. Ask questions** on `/copilot`. Use "NOT" for negation, "most" or "top N" for rankings, and "and" to combine topics. For historical data, set the reference time to after the data's last date.

**4. Approve or reject** proposed actions in the approval panel (admins only).

**5. Optional: grounding judge.** Off by default. It scores how well an answer is supported by its citations, using RAGAS and OpenAI. Enter an OpenAI key in the judge panel. The key is either used once or saved for 7, 30, or 90 days (saving requires the server secret below). Only the question, answer, and citation text go to OpenAI.

**6. Optional: LLM planner.** Ticks "LLM planner for unrecognized questions" on `/copilot`. It runs only when the server has the judge enabled and a key, and it only handles questions the rules can't place.

### Upload from the command line

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "content-type: application/json" \
  -d '{"email":"admin@acme.example","password":"<your-password>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -s -X POST "http://localhost:8000/api/v1/fleet/uploads?filename=fleet.ndjson" \
  -H "authorization: Bearer $TOKEN" -H "content-type: application/x-ndjson" \
  --data-binary @fleet.ndjson
```

### Ask from the command line

```bash
curl -s -X POST http://localhost:8000/api/v1/copilot/query \
  -H "authorization: Bearer $TOKEN" -H "content-type: application/json" \
  -d '{"question":"Which devices are NOT low on disk space?","allow_remediation":false}'
```

## Configuration

Set these on the backend.

| Variable | Purpose |
|---|---|
| `AFC_BOOTSTRAP_ADMIN_KEY` | Creates and deletes tenants. Required for tenant management. |
| `AFC_CORS_ORIGINS` | Comma-separated frontend origins allowed to call the API. Defaults to localhost:3000. |
| `AFC_JUDGE_KEY_SECRET` | Encrypts saved OpenAI keys. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Without it, saving is disabled. |
| `AFC_RAGAS_ENABLED` | `1` enables the server-side judge and LLM planner. Needs `OPENAI_API_KEY`. |
| `OPENAI_API_KEY` | Server-side OpenAI key for the judge and planner. |
| `AFC_API_KEYS` | Optional service keys as `key=tenant` pairs, for scripts. |
| `AFC_DEMO_ACCOUNT_PASSWORD` | Creates the local `demo` tenant and admin login (`admin@demo.local`). Local use only. |

On the frontend, `NEXT_PUBLIC_API_BASE_URL` sets the backend address (default `http://localhost:8000`).

## Deploy

The frontend is deployed on Vercel: https://agentic-fleet-copilot-ui.vercel.app

To redeploy:

```bash
cd frontend
npx vercel link --yes --project agentic-fleet-copilot-ui
npx vercel deploy --prod --yes
```

Vercel hosts the frontend only. The backend keeps state in SQLite and streams answers over Server-Sent Events, so it needs a host that runs a long-lived process (for example Render or Fly.io).

To connect the deployed frontend to a backend:

1. Start the backend with the frontend's origin allowed:
   `AFC_CORS_ORIGINS=https://agentic-fleet-copilot-ui.vercel.app,http://localhost:3000 uvicorn app.main:app --port 8000`
2. Build the frontend with `NEXT_PUBLIC_API_BASE_URL` pointing at that backend, then redeploy.

**If GitHub is unavailable,** the frontend can still be redeployed from your local copy of the `frontend/` folder with the commands above. The Vercel deployment itself is not a source backup, so keep a copy of the repository.

## Tests

```bash
cd backend && pytest -q                 # 221 deterministic tests, no network
cd frontend && npm run typecheck && npm run lint && npm run build
```

Live judge test (uses your key, makes network calls):

```bash
cd backend
OPENAI_API_KEY=sk-... AFC_RAGAS_ENABLED=1 pytest -m ragas -q
```

## Limits

- Tool actions (upgrade, ticket, replacement, notice) are stubs. They don't call real systems yet.
- Questions are answered from a fixed set of analyses. Anything outside them is refused, unless the LLM planner is enabled.
- Battery end-of-life can't be assessed when battery health isn't reported.
- Pending approvals survive restarts. Login throttling is per process, in memory.
