"""Load test (M6.5): how many conversations and live calls one server takes
before it slows down or fails, and where. Free: the model and speech are
simulated (backend/app/agent/simulated.py, backend/app/voice/simulated_live.py),
timed like the real services.

It starts the server in a subprocess on a free port with a fresh, migrated
database (SQLite, or the tests' throwaway Postgres with --postgres), seeded
with one synthetic customer per virtual caller so per-customer limits never
skew the result. Then, level by level, that many callers run at once: typed
conversations, then live calls that stream audio in real time and report
playback like the browser page. Per level: turns, latency p50/p95/max, the
overhead (latency minus the simulated model and speech time), errors by kind,
and the server's own timings for the same window.

Usage, from the repo root:
    python -m scripts.load_test [--typed 1,5,10,20,40,80] [--live 1,5,10,20,40] [--turns 3] [--postgres]
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import websockets
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.agent.simulated import ANSWER_S, DECIDE_S, looks_up
from backend.app.auth.session_tokens import issue_session_token
from backend.app.db.models import ConversationEventRecord, CustomerRecord, LiveTurnMetricRecord
from backend.app.db.session import upgrade_to_head
from backend.app.schemas.domain import AccountStatus
from backend.app.services.accounts import create_account
from backend.app.voice.simulated_live import FIRST_AUDIO_S, TURN_AUDIO_S
from scripts.live_report import percentile

REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "loadtest-results"
FRAME_S = 0.08
FRAME = bytes(int(16_000 * FRAME_S) * 2)  # 80 ms of silence at 16 kHz, 16-bit
REQUEST_TIMEOUT_S = 120


# --- the numbers --------------------------------------------------------------------


def expected_s(caller_turn: int, speed: float) -> float:
    """The simulated model time of a caller turn."""
    return ((DECIDE_S + ANSWER_S) if looks_up(caller_turn) else ANSWER_S) / speed


def spread(values: list[float]) -> dict:
    if not values:
        return {"p50": None, "p95": None, "max": None, "n": 0}
    return {"p50": round(statistics.median(values), 3), "p95": round(percentile(values, 0.95), 3),
            "max": round(max(values), 3), "n": len(values)}


def summarize_level(kind: str, callers: int, turns: list[dict], speed: float) -> dict:
    ok = [t for t in turns if t["error"] is None]
    return {
        "kind": kind,
        "callers": callers,
        "turns_ok": len(ok),
        "errors": dict(Counter(t["error"] for t in turns if t["error"] is not None)),
        "latency_s": spread([t["latency_s"] for t in ok]),
        "overhead_s": spread([t["latency_s"] - t["expected_s"] for t in ok]),
    }


# --- the server -------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def prepare_database(url: str, customers: int) -> None:
    upgrade_to_head(url)
    engine = create_engine(url)
    with sessionmaker(bind=engine)() as db:
        for n in range(customers):
            customer_id = f"cust_load_{n}"
            db.add(CustomerRecord(customer_id=customer_id, display_name="Synthetic Load Customer"))
            db.commit()
            create_account(db, account_id=f"acct_load_{n}", customer_id=customer_id, balance=Decimal("500.00"),
                           currency="USD", status=AccountStatus.ACTIVE, label="main")
    engine.dispose()


def start_server(url: str, port: int, secret: str, speed: float) -> subprocess.Popen:
    env = {**os.environ, "COLLECTWISE_DATABASE_URL": url, "COLLECTWISE_SESSION_SECRET": secret,
           "COLLECTWISE_LOAD_TEST": "1", "COLLECTWISE_AGENT_LLM": "simulated", "COLLECTWISE_SPEECH": "simulated",
           "COLLECTWISE_SIMULATED_SPEED": str(speed)}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health/ready", timeout=1).status_code == 200:
                return server
        except httpx.HTTPError:
            pass
        if server.poll() is not None:
            raise RuntimeError(f"the server exited: {server.stderr.read()[-2000:]}")
        time.sleep(0.2)
    server.terminate()
    raise RuntimeError("the server didn't become ready in 30 s")


# --- callers ----------------------------------------------------------------------------


def _error(exc: BaseException) -> str:
    return "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else type(exc).__name__


async def typed_caller(base: str, token: str, turns: int, speed: float) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    results = []
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=REQUEST_TIMEOUT_S) as client:
        try:
            created = await client.post("/agent/sessions")
        except httpx.HTTPError as exc:
            return [{"turn": 0, "latency_s": 0.0, "expected_s": 0.0, "error": _error(exc)}]
        if created.status_code != 201:
            return [{"turn": 0, "latency_s": 0.0, "expected_s": 0.0, "error": f"http_{created.status_code}"}]
        session_id = created.json()["session_id"]
        for turn in range(1, turns + 1):
            started = time.monotonic()
            try:
                response = await client.post(f"/agent/sessions/{session_id}/turns", json={"text": "What's my balance?"})
                error = None if response.status_code == 200 else f"http_{response.status_code}"
            except httpx.HTTPError as exc:
                error = _error(exc)
            results.append({"turn": turn, "latency_s": time.monotonic() - started,
                            "expected_s": expected_s(turn, speed), "error": error})
    return results


async def live_caller(base: str, token: str, turns: int, speed: float) -> list[dict]:
    """A polite caller: speaks a turn in real time, waits for the whole
    reply, and reports playback like the page (as if played at once)."""
    results = []
    async with httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:
        created = await client.post("/agent/sessions")
    if created.status_code != 201:
        return [{"turn": 0, "latency_s": 0.0, "expected_s": 0.0, "error": f"http_{created.status_code}"}]
    url = base.replace("http", "ws", 1) + f"/agent/sessions/{created.json()['session_id']}/live"
    try:
        async with websockets.connect(url, max_size=None, open_timeout=30) as socket_:
            await socket_.send(json.dumps({"type": "start", "token": token}))
            ready = json.loads(await asyncio.wait_for(socket_.recv(), 30))
            if ready.get("type") != "ready":
                return [{"turn": 0, "latency_s": 0.0, "expected_s": 0.0, "error": "not_ready"}]
            for turn in range(1, turns + 1):
                # Always real time: the server cuts off audio sent faster (H/D83).
                for frame in range(round(TURN_AUDIO_S / FRAME_S)):
                    if frame:
                        await asyncio.sleep(FRAME_S)
                    await socket_.send(FRAME)
                finished = time.monotonic()  # the last frame ends the simulated turn
                first_audio = None
                try:
                    while True:
                        message = await asyncio.wait_for(socket_.recv(), REQUEST_TIMEOUT_S)
                        if isinstance(message, bytes):
                            first_audio = first_audio or time.monotonic()
                            continue
                        event = json.loads(message)
                        if event["type"] == "segment_end":
                            await socket_.send(json.dumps(
                                {"type": "segment_played", "reply": event["reply"], "segment": event["segment"]}))
                        elif event["type"] == "stop_playback":
                            await socket_.send(json.dumps({"type": "playback_stopped", "reply": event["reply"],
                                                           "played": []}))
                        elif event["type"] == "reply_done":
                            break
                        elif event["type"] == "call_ended":
                            raise ConnectionError("call ended early")
                    error = None if first_audio is not None else "no_audio"
                    latency = (first_audio or time.monotonic()) - finished
                except (TimeoutError, ConnectionError, websockets.ConnectionClosed) as exc:
                    error, latency = _error(exc), time.monotonic() - finished
                results.append({"turn": turn, "latency_s": latency,
                                "expected_s": expected_s(turn, speed) + FIRST_AUDIO_S / speed, "error": error})
                if error is not None:
                    break
            await socket_.send(json.dumps({"type": "hang_up"}))
    except (OSError, websockets.WebSocketException, TimeoutError) as exc:
        results.append({"turn": len(results) + 1, "latency_s": 0.0, "expected_s": 0.0, "error": _error(exc)})
    return results


# --- the server's own view of a window ------------------------------------------------------


def server_view(url: str, since: datetime, until: datetime) -> dict:
    engine = create_engine(url)
    with sessionmaker(bind=engine)() as db:
        events = db.scalars(select(ConversationEventRecord).where(
            ConversationEventRecord.created_at >= since, ConversationEventRecord.created_at <= until)).all()
        live = db.scalars(select(LiveTurnMetricRecord).where(
            LiveTurnMetricRecord.created_at >= since, LiveTurnMetricRecord.created_at <= until)).all()
    engine.dispose()
    ms = lambda values: spread([v / 1000 for v in values if v is not None])  # noqa: E731
    return {
        "model_call_s": ms([e.latency_ms for e in events if e.event_type == "model_call"]),
        "tool_call_s": ms([e.latency_ms for e in events if e.event_type == "tool_call"]),
        "turn_s": ms([e.latency_ms for e in events if e.event_type == "turn_completed"]),
        "live_first_audio_s": ms([m.first_audio_ms for m in live]),
    }


# --- the run --------------------------------------------------------------------------------


async def run_levels(base: str, url: str, typed: list[int], live: list[int], turns: int, speed: float,
                     secret: str) -> list[dict]:
    customer = iter(range(sum(typed) + sum(live)))
    token = lambda: issue_session_token(f"cust_load_{next(customer)}", secret=secret.encode(), ttl_seconds=3600)  # noqa: E731
    rows = []
    for kind, levels, caller in (("typed", typed, typed_caller), ("live", live, live_caller)):
        for callers in levels:
            since = datetime.now(timezone.utc)
            done = await asyncio.gather(*(caller(base, token(), turns, speed) for _ in range(callers)))
            until = datetime.now(timezone.utc)
            row = summarize_level(kind, callers, [t for result in done for t in result], speed)
            row["server"] = server_view(url, since, until)
            rows.append(row)
            print(format_row(row), flush=True)
    return rows


def format_row(row: dict) -> str:
    lat, over, tool = row["latency_s"], row["overhead_s"], row["server"]["tool_call_s"]
    errors = ", ".join(f"{k} {v}" for k, v in sorted(row["errors"].items())) or "none"
    show = lambda x: "-" if x is None else f"{x:6.2f}"  # noqa: E731
    return (f"{row['kind']:5} {row['callers']:>4} callers  turns {row['turns_ok']:>4}  "
            f"latency p50 {show(lat['p50'])} p95 {show(lat['p95'])} max {show(lat['max'])} s  "
            f"overhead p50 {show(over['p50'])} p95 {show(over['p95'])} s  "
            f"tool p95 {show(tool['p95'])} s  errors: {errors}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    levels = lambda text: [int(n) for n in text.split(",") if n]  # noqa: E731
    parser.add_argument("--typed", type=levels, default=[1, 5, 10, 20, 40, 80])
    parser.add_argument("--live", type=levels, default=[1, 5, 10, 20, 40])
    parser.add_argument("--turns", type=int, default=3, help="caller turns per conversation")
    parser.add_argument("--speed", type=float, default=1.0, help="simulated services run this many times faster")
    parser.add_argument("--postgres", action="store_true", help="the tests' throwaway Postgres instead of SQLite")
    parser.add_argument("--out", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # not a line per request

    cluster = None
    if args.postgres:
        from backend.tests.databases import ThrowawayPostgres

        cluster = ThrowawayPostgres()
        url = cluster.empty_database()
    else:
        url = f"sqlite:///{tempfile.mkdtemp(prefix='cw-load-')}/load.db"
    secret = secrets.token_urlsafe(48)
    server = None
    try:
        prepare_database(url, sum(args.typed) + sum(args.live))
        port = free_port()
        server = start_server(url, port, secret, args.speed)
        database = "postgres" if args.postgres else "sqlite"
        print(f"Load test on {database}, {args.turns} turns per caller, simulated speed x{args.speed:g}")
        rows = asyncio.run(run_levels(f"http://127.0.0.1:{port}", url, args.typed, args.live, args.turns,
                                      args.speed, secret))
    finally:
        if server is not None:
            server.terminate()
            server.wait(10)
        if cluster is not None:
            cluster.stop()
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out / f"load-{database}-{stamp}.json"
    out.write_text(json.dumps({"database": database, "turns": args.turns, "speed": args.speed, "levels": rows},
                              indent=2))
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
