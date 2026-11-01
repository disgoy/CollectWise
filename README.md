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
- SQLite (default) or Postgres via SQLAlchemy, with Alembic migrations

## Setup

Requires Python 3.11.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Database

The schema comes from migrations, never from the app: bring a database to the
latest version with

```bash
alembic upgrade head
```

A server on an older schema logs what to run and answers 503
`schema_out_of_date` on `/health/ready`; scripts stop with the same advice.

**A `collectwise.db` made before migrations existed** (before Milestone 6.4) is
adopted once, keeping its rows:

```bash
alembic stamp 0001
```

```bash
alembic upgrade head
```

**Postgres:** create a database of its own and point the app at it (the
default stays SQLite, `sqlite:///./collectwise.db`):

```bash
export COLLECTWISE_DATABASE_URL="postgresql+psycopg://localhost/collectwise"
```

then `alembic upgrade head`. To run the whole test suite on Postgres instead
of SQLite (it starts a throwaway cluster of its own with `initdb` and
`pg_ctl`, on a private socket, and deletes it afterwards; no other Postgres
server is touched):

```bash
COLLECTWISE_TEST_POSTGRES=1 python -m pytest -q
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

## Phone calls (Twilio)

A Twilio number can reach the same live agent (Milestone 7.1). Point the
number's voice webhook at `POST {COLLECTWISE_PUBLIC_BASE_URL}/telephony/twilio/voice`;
the app answers with a media stream to `/telephony/twilio/stream`. Phone audio
(8 kHz μ-law) goes to Deepgram and back to Twilio unconverted; Twilio's
playback marks feed the same heard-tracking as the browser, so an interrupted
read-back is read again. Only Twilio can start a call: every request must be
signed with your `TWILIO_AUTH_TOKEN`, and a stream must also present a
one-time pass the webhook issued for that call.

**Who is calling (7.2):** before the agent exists, the call is verified in
code: the caller's number must belong to a customer **and** they must type
that customer's 6-digit phone PIN on the keypad. While verifying, nothing the
caller says is transcribed; the digits are compared and dropped (never
logged, stored or shown to the model). Every failure sounds the same; 3
wrong PINs in a row lock the PIN for 15 minutes. Set a customer's number and
PIN with `PUT /customers/{id}/phone` (admin key), or for the demo customer:

```bash
python -m scripts.seed_demo --phone +15555550100
```

**Watching a phone call on screen:** sign in on `/live` as the same customer
and the page shows the call's captions, clock and timeline while you talk on
the phone. Only that customer's page sees them; nothing is stored.

**A real call from your phone (7.5):** a Twilio trial number pointed at your
laptop through a free ngrok tunnel, with no deployment and no number that's
always on. With `COLLECTWISE_PUBLIC_BASE_URL` set, requests to that host
reach only the webhook and the media stream; every other route is a 404
there and works as usual on `127.0.0.1`. `python -m scripts.phone_preflight`
checks the settings, your demo phone PIN and the tunnel (free), and prints
the webhook URL. The steps, what to try and how to measure it (`live_report
--phone`) are in [docs/PHONE_CALL_CHECKLIST.md](docs/PHONE_CALL_CHECKLIST.md).

## Operations MCP server

A read-only [MCP](https://modelcontextprotocol.io) server lets an assistant
such as Claude Desktop or Claude Code answer questions about the running
service from its own numbers: live-call speed, recent calls, conversation
outcomes and errors, eval results and database health, plus the failure,
data-handling and load-test documents. It runs as its own process over stdio
(no network port), opens the database read-only, is never loaded by the app
or offered to the agent, and returns counts, codes, timings and short call
references only: no customer ids, nothing callers said, no amounts.

To add it to Claude Code (replace the paths with yours):

```bash
claude mcp add collectwise-ops -e PYTHONPATH=/path/to/collectwise -e COLLECTWISE_DATABASE_URL=sqlite:////path/to/collectwise/collectwise.db -- /path/to/collectwise/.venv/bin/python -m backend.ops.mcp_server
```

For Claude Desktop, the same command, `args` and `env` go under `mcpServers`
in its configuration file.

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
| `TWILIO_AUTH_TOKEN` | unset (phone calls off) | Checks that phone-call requests really come from Twilio; keep it in `.env` |
| `COLLECTWISE_PUBLIC_BASE_URL` | unset (phone calls off) | The public https address Twilio calls; signatures are checked against it, and requests to its host reach only the phone routes |
| `COLLECTWISE_LOAD_TEST` | unset (off) | `1` allows `COLLECTWISE_AGENT_LLM=simulated` and `COLLECTWISE_SPEECH=simulated`, free stand-ins for load tests; never on a real server |
| `COLLECTWISE_LIVE_AGENT_THREADS` | `64` | Threads for live calls' agent turns: about one per simultaneous call |
| `COLLECTWISE_LOG_FORMAT` | `text` | `json` writes one JSON object per log line, with the trace id when tracing is on |
| `COLLECTWISE_TELEMETRY_RETENTION_DAYS` | `30` | `python -m scripts.purge_old_data --yes` deletes conversation events and timing rows older than this; servicing records are never purged |
| `COLLECTWISE_TRACING` | unset (off) | `console` prints trace spans; `otlp` sends them to `OTEL_EXPORTER_OTLP_ENDPOINT` (e.g. a local Jaeger). Spans hold codes and timings, never words |
| `COLLECTWISE_TURN_LOCK_WAIT_S` | `5` | A typed message waits this long for the previous turn, then gets 409 |
| `ANTHROPIC_API_KEY` | none | Anthropic credentials |
| `COLLECTWISE_SPEECH` | unset (voice off) | Set to `deepgram` to enable real, paid speech-to-text and text-to-speech |
| `COLLECTWISE_STT_MODEL` | `nova-3` | Deepgram speech-to-text model |
| `COLLECTWISE_TTS_VOICE` | `aura-2-thalia-en` | Deepgram voice for spoken replies |
| `COLLECTWISE_HINDI_STT_MODEL` | `flux-general-multi` | Live listening for Hindi calls |
| `COLLECTWISE_HINDI_LANGUAGE_HINTS` | `hi,en` | Languages that listener expects |
| `COLLECTWISE_HINDI_TTS_VOICE` | `flux-rahul-hi` | Voice for Hindi calls (a `flux-*` voice, on `/v2/speak`) |
| `COLLECTWISE_HINDI_KEYTERMS` | off | `1` sends account names to the Hindi listener as keyterms (`smoke_live_speech --hindi` checks it takes them) |
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
| `COLLECTWISE_DEMO_CUSTOMERS` | `cust_demo,cust_demo_in` | Comma-separated synthetic customers demo sign-in may act as |
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
and allow the microphone.

**In Hindi (Milestone 7.6):** choose **हिंदी** before Start call, ideally signed in as
*Demo Customer (India)* (rupee accounts). The conversation is then held in Hindi and Hinglish
throughout, with the same rules: Deepgram's multilingual listener (`flux-general-multi`, hints
Hindi and English) hears it, the agent answers in Devanagari with amounts in digits, and a Hindi
voice (`flux-rahul-hi`) speaks it. The choice is fixed for the call. Typed conversations take
`{"language": "hi"}` when they start; turn-based voice turns are English only, and phone calls
too. Measured in [evals/voice_checks/README.md](evals/voice_checks/README.md) (`run_hindi_checks`). Headphones work best. The page is plain HTML and
JavaScript served by the app with a strict Content-Security-Policy; its logic
is tested with `node --test backend/tests/js/` (also run by pytest).

How many calls one server takes, and where it slows down: [docs/LOAD_TEST.md](docs/LOAD_TEST.md)
(`python -m scripts.load_test`, free: simulated model and speech).

What is stored where, for how long, and what never is: [docs/DATA_HANDLING.md](docs/DATA_HANDLING.md).

What happens when each part fails (the model, listening, speaking, the database, the caller's
connection): [docs/FAILURE_MODES.md](docs/FAILURE_MODES.md), each row proven by a test.

**The evidence page:** http://127.0.0.1:8000/evidence shows the evidence report's charts with
live-call speed from this server's own calls (the rest from local result files or the committed
snapshot, each section labelled). Its data, `GET /evidence/data`, is open and holds aggregates only.

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
