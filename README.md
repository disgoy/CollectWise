# CollectWise

A reliability-focused customer-servicing backend, being extended step by step
into a voice AI servicing agent. All data is synthetic, and no real money moves:
a servicing request is evaluated and recorded, never executed.

## What exists today

- FastAPI API for customers, accounts and servicing requests
- Deterministic policy engine (`backend/app/policies/servicing.py`) that
  decides allow/deny with a reason code
- Idempotent servicing requests: a retry with the same `request_id` returns
  the stored decision; the same ID with different details returns 409
- Audit trail saved in the same transaction as each decision
- SQLite database via SQLAlchemy

## Setup

Requires Python 3.11.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Run the API

```bash
uvicorn backend.app.main:app --reload
```

Then open http://127.0.0.1:8000/docs for the interactive API docs.

## Run the tests

```bash
python -m pytest -q
```

Tests use their own temporary databases and never touch `collectwise.db`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `COLLECTWISE_DATABASE_URL` | `sqlite:///./collectwise.db` | Database connection URL |
| `COLLECTWISE_SESSION_SECRET` | none (required for the agent API) | Signs caller session tokens; at least 32 characters |
| `COLLECTWISE_AGENT_LLM` | unset (agent off) | Set to `anthropic` to enable real, paid model calls |
| `COLLECTWISE_AGENT_MODEL` | `claude-sonnet-5-5` | Model used by the agent |
| `COLLECTWISE_AGENT_EFFORT` | `medium` | Model effort level |
| `ANTHROPIC_API_KEY` | none | Anthropic credentials |

Local secrets go in a git-ignored `.env` (template: `.env.example`). The app
does not read it itself; load it into your shell with `set -a; source .env; set +a`.

## API overview

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/customers` | Create a customer |
| GET | `/customers/{customer_id}` | Get a customer |
| GET | `/customers/{customer_id}/accounts` | List a customer's accounts |
| POST | `/accounts` | Create an account |
| GET | `/accounts/{account_id}` | Get an account |
| POST | `/servicing/requests` | Evaluate a servicing request |
| GET | `/servicing/requests/{request_id}` | Get a stored servicing request |

## Roadmap

1. Extract the servicing workflow into a reusable service layer
2. Verified caller identity and a narrow, policy-gated tool layer
3. Text agent with tool calling and evaluations
4. Turn-based voice (speech-to-text and text-to-speech adapters)
5. Real-time streaming voice over WebSockets, with interruption handling
6. Production readiness: tracing, latency budgets, failure testing, Postgres
7. Telephony
