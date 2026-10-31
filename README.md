# CollectWise

A reliability-focused customer-servicing backend, being extended step by step
into a voice AI servicing agent. All data is synthetic, and no real money moves:
a servicing request is evaluated and recorded, never executed.

**Design write-up:** [docs/DESIGN.md](docs/DESIGN.md): architecture, key decisions, engineering challenges and measured results.

**Evidence dashboard:** [CollectWise Evidence](https://claude.ai/artifact/MKYnNm7ioC612k77Uy7KYW): agent eval by version, error analysis, hearing accuracy, live-call speed and test growth, in charts. Its data is rebuilt from the real result files by `python -m scripts.build_evidence` (aggregates only, never transcripts) into [`evidence/`](evidence/).

## What exists today

- FastAPI API for customers, accounts and servicing requests
- Deterministic policy engine (`backend/app/policies/servicing.py`) that
  decides allow/deny with a reason code
- Idempotent servicing requests: a retry with the same `request_id` returns
  the stored decision; the same ID with different details returns 409
- Audit trail saved in the same transaction as each decision
- Back-office routes (`/customers`, `/accounts`, `/servicing`) need an admin
  key; callers reach the agent routes with a signed session token
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

To measure line and branch coverage (settings in `.coveragerc`):

```bash
python -m pytest --cov --cov-report=term-missing
```

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
| `COLLECTWISE_ADMIN_KEY` | none (back-office API refuses every request) | Key for the back-office routes, sent as the `X-Admin-Key` header; at least 32 characters |
| `COLLECTWISE_AGENT_LLM` | unset (agent off) | Set to `anthropic` to enable real, paid model calls |
| `COLLECTWISE_AGENT_MODEL` | `claude-sonnet-5-5` | Model used by the agent |
| `COLLECTWISE_AGENT_EFFORT` | `medium` | Model effort level |
| `COLLECTWISE_AGENT_TIMEOUT_S` | `15` | Longest wait for the next piece of a model reply before the caller hears "having trouble" |
| `COLLECTWISE_AGENT_MAX_RETRIES` | `1` | Retries of a failed model call |
| `COLLECTWISE_TURN_LOCK_WAIT_S` | `5` | A typed message waits this long for the previous turn, then gets 409 |
| `ANTHROPIC_API_KEY` | none | Anthropic credentials |
| `COLLECTWISE_SPEECH` | unset (voice off) | Set to `deepgram` to enable real, paid speech-to-text and text-to-speech |
| `COLLECTWISE_STT_MODEL` | `nova-3` | Deepgram speech-to-text model |
| `COLLECTWISE_TTS_VOICE` | `aura-2-thalia-en` | Deepgram voice for spoken replies |
| `DEEPGRAM_API_KEY` | none | Deepgram credentials |
| `COLLECTWISE_SESSION_IDLE_MINUTES` | `15` | A conversation unused this long expires |
| `COLLECTWISE_MAX_SESSIONS_PER_CUSTOMER` | `3` | Open conversations per customer; a new one replaces their least recently used |
| `COLLECTWISE_MAX_SESSIONS` | `1000` | Open conversations in total; beyond it new ones get 503 |
| `COLLECTWISE_MAX_CALLER_TURNS` | `30` | Caller turns per conversation before it is handed to a person |
| `COLLECTWISE_TURNS_PER_MINUTE` | `10` | Turns (typed and spoken together) per customer per minute; beyond it 429 |
| `COLLECTWISE_SESSION_STARTS_PER_MINUTE` | `5` | New conversations per customer per minute; beyond it 429 |
| `COLLECTWISE_PAYMENT_CONFIRM_TURNS` | `2` | A proposed payment can be confirmed only within this many further caller turns |
| `COLLECTWISE_PAYMENT_CONFIRM_MINUTES` | `5` | ...and within this many minutes; after that the agent proposes it again |
| `COLLECTWISE_DEMO_LOGIN` | unset (off) | `1` turns on demo sign-in for the live-call page; local only, never on a public server |
| `COLLECTWISE_DEMO_CUSTOMERS` | `cust_demo` | Comma-separated synthetic customers demo sign-in may act as |
| `COLLECTWISE_VOICE_MIN_CONFIDENCE` | `0.6` | Voice turns heard with lower speech-to-text confidence get "say that again" instead of reaching the agent |

To hear the voice turn end to end (real Deepgram and Claude, a few cents):
`COLLECTWISE_AGENT_LLM=anthropic COLLECTWISE_SPEECH=deepgram python -m scripts.smoke_voice`.

Local secrets go in a git-ignored `.env` (template: `.env.example`). The app
does not read it itself; load it into your shell with `set -a; source .env; set +a`.

## API overview

There are two kinds of route. **Back-office** routes (customers, accounts,
servicing) are for operators and setup: they need the `X-Admin-Key` header
and answer 503 if no key is configured. **Agent** routes are for callers: they
need `Authorization: Bearer <session token>`, and the caller's identity comes
only from that token, and they are rate limited per customer (429 with
`Retry-After`). Conversations live in memory: idle ones expire and all are
lost on restart. Only `/health` and `/health/ready` are open. Plan for deployment: only the
agent routes and `/health` should be reachable from the internet.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness: the process answers (no dependencies) |
| GET | `/health/ready` | Readiness: the database answers; 503 if not |
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
| WS | `/agent/sessions/{session_id}/live` | A live voice call: the first message carries the session token; 16 kHz PCM in, captions, sentences and 24 kHz audio out; the caller can interrupt |

### Try a live call in the browser

Real (paid) Deepgram and Claude calls; synthetic data only.

```bash
python -m scripts.seed_demo
set -a; source .env; set +a
COLLECTWISE_DEMO_LOGIN=1 COLLECTWISE_AGENT_LLM=anthropic COLLECTWISE_SPEECH=deepgram uvicorn backend.app.main:app
```

`.env` needs `COLLECTWISE_SESSION_SECRET` (32+ random characters) besides the API keys: it signs
the sign-in tokens.

Open http://127.0.0.1:8000/live, sign in as the demo customer, press Start call
and allow the microphone. Headphones work best. The page is plain HTML and
JavaScript served by the app with a strict Content-Security-Policy; its logic
is tested with `node --test backend/tests/js/` (also run by pytest).

What happens when each part fails (the model, listening, speaking, the database, the caller's
connection): [docs/FAILURE_MODES.md](docs/FAILURE_MODES.md), each row proven by a test.

To measure a call: [docs/LIVE_CALL_CHECKLIST.md](docs/LIVE_CALL_CHECKLIST.md) (what to try), then
`python -m scripts.live_report --minutes 30` (speed and hearing, numbers only) and
`run_voice_checks --live` (the live confidence threshold, through Flux).

### Live call protocol

`WS /agent/sessions/{session_id}/live`. The first message must be
`{"type": "start", "token": "<session token>"}` within 5 seconds; then the
server sends `{"type": "ready", "sample_rate_in": 16000, "sample_rate_out": 24000}`.

- Caller to server: binary frames of mono 16-bit PCM at 16 kHz (80 ms
  recommended, at most 200 ms, no faster than real time);
  `{"type": "segment_played", "reply", "segment"}` when a segment has played;
  `{"type": "playback_stopped", "reply", "played": [...]}` after a stop;
  `{"type": "hang_up"}`. Unknown types are ignored.
- Server to caller: `caller_speaking` (the listener heard the caller begin), `caption`, `caller_said`, `agent_sentence`, `segment_end`,
  `stop_playback`, `reply_done`, `call_ended` as JSON; audio as binary frames
  with an 8-byte header (reply, segment as little-endian uint32) followed by
  24 kHz PCM.
- Close codes: 1000 normal, 4400 malformed or audio too fast, 4401 bad token,
  4404 session not found, 4408 no start in time, 4409 already live,
  4503 not configured.

## Roadmap

1. Extract the servicing workflow into a reusable service layer
2. Verified caller identity and a narrow, policy-gated tool layer
3. Text agent with tool calling and evaluations
4. Turn-based voice (speech-to-text and text-to-speech adapters)
5. Real-time streaming voice over WebSockets, with interruption handling
6. Production readiness: tracing, latency budgets, failure testing, Postgres
7. Telephony
