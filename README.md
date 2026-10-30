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

## Evaluate the agent

`evals/agent_conversations/` plays 25 scripted conversations (payments,
account facts, identity attacks, handoffs) through the real agent loop, each
against its own in-memory database, and grades them three ways:

- **outcome**: exactly the expected payment requests were recorded, and the
  conversation was (or wasn't) handed to a human as expected
- **compliance**: nothing touched another customer's account, and the agent
  never tried to submit a payment before the caller confirmed it
- **quality**: a judge model (Claude Haiku 5.5) checks replies for grounded
  facts, no "paid" claims, no internal IDs, plain language, no over-promising
  and responsiveness

The headline, `all_pass`, needs all three. Free offline checks with scripted
models (an oracle that must score 100%, and do-nothing agents) run with the
test suite. A real run calls the API and costs money (the 50-conversation
baseline cost about $0.23):

```bash
set -a; source .env; set +a
COLLECTWISE_AGENT_LLM=anthropic python -m evals.agent_conversations.run_eval --reps 2
```

Results land in `.claude/hillclimb/agent_conversations/baseline/`. The runner
refuses paid runs until a person approves the harness with `--approve-harness`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `COLLECTWISE_DATABASE_URL` | `sqlite:///./collectwise.db` | Database connection URL |
| `COLLECTWISE_SESSION_SECRET` | none (required for the agent API) | Signs caller session tokens; at least 32 characters |
| `COLLECTWISE_AGENT_LLM` | unset (agent off) | Set to `anthropic` to enable real, paid model calls |
| `COLLECTWISE_AGENT_MODEL` | `claude-sonnet-5-5` | Model used by the agent |
| `COLLECTWISE_AGENT_EFFORT` | `medium` | Model effort level |
| `ANTHROPIC_API_KEY` | none | Anthropic credentials |
| `COLLECTWISE_VOICE_MIN_CONFIDENCE` | `0.5` | Voice turns heard with lower speech-to-text confidence get "say that again" instead of reaching the agent |

Local secrets go in a git-ignored `.env` (template: `.env.example`). The app
does not read it itself; load it into your shell with `set -a; source .env; set +a`.

## API overview

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/customers` | Create a customer |
| GET | `/customers/{customer_id}` | Get a customer |
| GET | `/customers/{customer_id}/accounts` | List a customer's accounts |
| POST | `/accounts` | Create an account, optionally with a spoken `label` such as "savings" (unique per customer) |
| GET | `/accounts/{account_id}` | Get an account |
| POST | `/servicing/requests` | Evaluate a servicing request |
| GET | `/servicing/requests/{request_id}` | Get a stored servicing request |
| POST | `/agent/sessions` | Start a conversation (Bearer session token) |
| POST | `/agent/sessions/{session_id}/turns` | Send a typed caller turn |
| POST | `/agent/sessions/{session_id}/voice-turns` | Send a spoken caller turn (WAV body); returns transcript, reply and base64 WAV audio |

## Roadmap

1. Extract the servicing workflow into a reusable service layer
2. Verified caller identity and a narrow, policy-gated tool layer
3. Text agent with tool calling and evaluations
4. Turn-based voice (speech-to-text and text-to-speech adapters)
5. Real-time streaming voice over WebSockets, with interruption handling
6. Production readiness: tracing, latency budgets, failure testing, Postgres
7. Telephony
