# Load test (Milestone 6.5)

How many conversations and live calls one server process takes before it
slows down, and why. Measured 2026-11-01 on an 8-core Mac with
`python -m scripts.load_test` (and `--postgres`), using a simulated model and
simulated speech timed like the real ones (a tool-deciding model call 1.6 s,
an answering call 1.1 s, first audio 0.3 s; two caller turns in three use a
lookup). Each virtual caller is its own synthetic customer and has 3 turns.
Free to run: no real model or speech service is called.

"Overhead" is the time beyond the simulated model and speech work: waiting
inside our server. "Tool p95" is the server's own timing of tool calls, where
waiting for a database connection shows up.

## Results before the fixes

### Typed conversations

| At once | SQLite p50 / p95 latency | SQLite overhead p95 | Postgres p50 / p95 latency | Postgres overhead p95 | Tool p95 (SQLite / Postgres) | Errors |
|---|---|---|---|---|---|---|
| 1 | 2.72 / 2.73 s | 0.03 s | 2.72 / 2.73 s | 0.03 s | 0.00 / 0.01 s | none |
| 5 | 2.73 / 2.82 s | 0.12 s | 2.73 / 2.74 s | 0.04 s | 0.00 / 0.00 s | none |
| 10 | 2.72 / 2.76 s | 0.06 s | 2.74 / 2.76 s | 0.06 s | 0.00 / 0.01 s | none |
| 20 | 2.72 / 3.79 s | **1.09 s** | 2.75 / 3.85 s | **1.15 s** | **1.07 / 1.11 s** | none |
| 40 | 2.73 / 4.84 s | **2.14 s** | 2.77 / 4.94 s | **2.24 s** | **2.12 / 2.21 s** | none |
| 80 | 4.46 / 6.62 s | **3.92 s** | 5.48 / 8.34 s | **5.64 s** | 2.13 / 2.18 s | none |

### Live calls (latency = caller finishes → first audio)

| At once | SQLite p50 / p95 | Overhead p50 / p95 | Postgres p50 / p95 | Overhead p50 / p95 | Errors |
|---|---|---|---|---|---|
| 1 | 3.03 / 3.03 s | 0.03 / 0.03 s | 3.02 / 3.02 s | 0.02 / 0.03 s | none |
| 5 | 3.02 / 3.03 s | 0.02 / 0.03 s | 3.03 / 3.08 s | 0.03 / 0.08 s | none |
| 10 | 3.02 / 3.04 s | 0.02 / 0.04 s | 3.04 / 3.05 s | 0.04 / 0.05 s | none |
| 20 | 3.03 / 5.73 s | 0.03 / **2.73 s** | 3.04 / 5.76 s | 0.04 / **2.76 s** | none |
| 40 | 5.17 / 8.44 s (max 11.1) | **2.18 / 5.44 s** | 5.17 / 8.49 s (max 11.2) | **2.18 / 5.49 s** | none |

Nothing failed at any level: no 5xx, no timeouts, no rate limiting.

## Where it saturates, and why

The same thresholds on SQLite and Postgres: the limits are in our process,
not the database.

1. **A typed turn holds a database connection through its model calls.**
   The request's database session takes a connection at its first lookup
   and keeps it until the request ends, so through the ~1.1 s answering call
   that follows. The pool has 15 connections (SQLAlchemy's default 5 + 10).
   From about 15 simultaneous lookups, the next lookup waits for a connection
   to come back: the tool p95 is ~1.1 s at 20 callers (one answering call)
   and ~2.2 s at 40 (two). The tools themselves take about 10 ms.
2. **Live turns share a pool of 12 worker threads.** Every live call's agent
   turn runs in `asyncio.to_thread`, the event loop's default executor:
   `min(32, cores + 4)` = 12 workers here, also used for each call's timing
   and note writes. Up to 10 calls at once nothing waits; at 20, the turns
   beyond 12 wait for a whole earlier turn (~2.7 s at p95); at 40, the median
   turn waits 2.2 s.
3. **Typed requests share 40 threads.** FastAPI runs sync routes on anyio's
   40-thread pool, so at 80 simultaneous turns half wait for a whole turn
   (the overhead p50 jumps to ~2.9 s), on top of (1).

Not seen at these loads: SQLite write-lock errors, the 30 s pool wait
running out, event-loop slowdown.

## The fixes, and the results after them

Two fixes, made and measured the same day:

- **The connection goes back after each tool.** Services commit their own
  writes, so after a tool returns only a read transaction is open; the agent
  loop now ends it (a rollback, so a write left unsaved by mistake is
  dropped, never saved half-done). A turn holds a connection for about
  10 ms instead of through its model calls.
- **Live agent turns have their own pool** of `COLLECTWISE_LIVE_AGENT_THREADS`
  threads (64), and the quick note and timing writes stay on the default
  executor, no longer behind slow turns. Tracing context is carried across.

| At once | Typed overhead p95, before → after (SQLite / Postgres) | Tool p95 after | Live overhead p95, before → after (SQLite / Postgres) |
|---|---|---|---|
| 10 | 0.06 → 0.07 s / 0.06 → 0.07 s | 0.01 s | 0.04 → 0.06 s / 0.05 → 0.06 s |
| 20 | **1.09 → 0.12 s / 1.15 → 0.07 s** | 0.00–0.02 s | **2.73 → 0.23 s / 2.76 → 0.16 s** |
| 40 | **2.14 → 0.14 s / 2.24 → 0.16 s** | 0.01–0.03 s | **5.44 → 0.06 s / 5.49 → 0.09 s** |
| 80 (typed) | 3.92 → 2.88 s / 5.64 → 5.62 s | 0.01–0.02 s | — |

Still no errors at any level. One process now takes **40 typed conversations
and 40 live calls at once with no meaningful wait** (live up to the 64-thread
pool). What remains at 80 typed conversations is the third limit: FastAPI's
40 threads for typed requests, so half the turns wait for a whole turn
(overhead p50 ~2.7–2.8 s). Next if wanted: that thread count and the
database pool size as settings, sized together (Postgres allows 100
connections by default, shared across processes).

## Beyond one process

Conversations, live-call registration and rate limits live in this process's
memory (D72), so the server must run as **one process**. Growing past one
process or machine would need:

- a shared store for conversations, pending payments and rate limits (for
  example Redis), with the per-conversation lock becoming a distributed lock;
- live calls routed back to the process holding their WebSocket (sticky
  routing), or the call state moved into the shared store;
- Postgres (6.4) instead of SQLite, with a connection budget per process.

None of this is built.

## Not proven

Real model and speech latency under load (the simulation uses measured
medians; real services vary, have rate limits and can slow down under load
themselves), network conditions, and anything across processes or machines.
