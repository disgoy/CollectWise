"""Eval runner for the CollectWise agent.

Plays each scripted conversation in cases.json through the real agent loop
(run_turn, the same call the /agent turns endpoint makes) against a fresh
in-memory database, then grades what it left behind and how it spoke.

Results go to .claude/hillclimb/agent_conversations/<variant>/:
results.jsonl (one row per case and rep, written as each finishes),
traces/<id>_rep<k>.json and errors.jsonl (attempts that produced nothing to
score). A rerun skips (case, rep) pairs already in results.jsonl.

Paid runs (the real model, or the judge) need COLLECTWISE_AGENT_LLM=anthropic
and a harness approved by a person with --approve-harness. Offline runs
(--judge off with a scripted --agent) cost nothing. Scripted agents always
need --out.

Usage, from the repo root:
    python -m evals.agent_conversations.run_eval --agent oracle --judge off --out DIR
    set -a; source .env; set +a
    COLLECTWISE_AGENT_LLM=anthropic python -m evals.agent_conversations.run_eval \\
        --cases pay_happy_path,handoff_dispute --reps 1
"""

import argparse
import hashlib
import json
import logging
import math
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app import config
from backend.app.agent.claude import ClaudeClient
from backend.app.agent.llm import LLMUnavailableError
from backend.app.agent.loop import run_turn
from backend.app.agent.prompts import SYSTEM_PROMPT
from backend.app.agent.sessions import SessionStore
from backend.app.db import models
from backend.app.db.base import Base
from backend.app.schemas.domain import AccountStatus, ServicingOperation, ServicingRequest
from backend.app.services.accounts import create_account
from backend.app.services.servicing import evaluate_servicing_request
from evals.agent_conversations import grading
from evals.agent_conversations.oracle import CONSTANT_REPLIES, ConstantLLM, oracle_llm

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = Path(__file__).with_name("cases.json")
FLOW_DIR = REPO_ROOT / ".claude" / "hillclimb" / "agent_conversations"

MAX_RETRIES = 4  # per model call, on transient API errors

# USD per million tokens, for the printed summary only. Cache writes cost
# 1.25x input, cache reads 0.1x.
PRICES = {
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00},
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50},
}


class HarnessError(Exception):
    """An attempt that produced nothing to score. Goes to errors.jsonl."""

    def __init__(self, failure_class: str, detail: str, *, calls: list | None = None) -> None:
        super().__init__(detail)
        self.failure_class = failure_class
        self.detail = detail
        self.calls = calls or []


# --- the model, wrapped -----------------------------------------------------


class RecordingLLM:
    """Wraps the agent's LLM client: records model, usage and stop reason per
    call, retries transient failures with jittered backoff, enforces the
    case's wall-clock ceiling, and checks the model that actually answered."""

    def __init__(self, inner: Any, *, expected_model: str | None, deadline: float) -> None:
        self.inner = inner
        self.expected_model = expected_model
        self.deadline = deadline
        self.calls: list[dict] = []
        self.retries = 0
        self.gave_up = False

    def create(self, *, system: str, tools: list[dict], messages: list[dict]) -> Any:
        for attempt in range(MAX_RETRIES + 1):
            if time.monotonic() > self.deadline:
                raise HarnessError("timeout", "case wall-clock ceiling reached", calls=self.calls)
            started = time.perf_counter()
            try:
                response = self.inner.create(system=system, tools=tools, messages=messages)
                break
            except LLMUnavailableError:
                if attempt == MAX_RETRIES:
                    self.gave_up = True
                    raise  # the loop turns this into an "unavailable" turn
                self.retries += 1
                time.sleep(min(30, 2 ** attempt) * random.uniform(0.5, 1.5))

        model = getattr(response, "model", None)
        usage = getattr(response, "usage", None)
        self.calls.append({
            "model": model,
            "stop_reason": response.stop_reason,
            "latency_s": round(time.perf_counter() - started, 3),
            "usage": {
                key: (getattr(usage, key, None) or 0) if usage is not None else 0
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            },
        })
        if self.expected_model and not (model or "").startswith(self.expected_model):
            raise HarnessError(
                "served_model_mismatch",
                f"answered by {model}, expected {self.expected_model}",
                calls=self.calls,
            )
        return response


# --- one case ---------------------------------------------------------------


def _enable_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def new_database() -> Session:
    """A private in-memory database, so cases can't see each other's data."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    Base.metadata.create_all(engine)
    return Session(engine, autoflush=False, expire_on_commit=False)


def seed(db: Session, world: dict, case: dict) -> set[str]:
    """Create the world's customers, then their accounts (with labels) and the
    case's earlier payment requests through the real services. Returns the
    earlier requests' IDs."""
    for customer_id in world["customers"]:
        db.add(models.CustomerRecord(customer_id=customer_id, display_name="Synthetic Customer"))
    db.commit()
    for customer_id, accounts in world["customers"].items():
        for account in accounts:
            create_account(
                db,
                account_id=account["account_id"],
                customer_id=customer_id,
                balance=Decimal(account["balance"]),
                currency=account["currency"],
                status=AccountStatus(account["status"]),
                label=account.get("label"),
            )

    history_ids = set()
    for index, payment in enumerate(case.get("history", [])):
        request_id = f"pay_history_{index}"
        decision = evaluate_servicing_request(
            db,
            ServicingRequest(
                request_id=request_id,
                customer_id=case["customer"],
                account_id=payment["account_id"],
                operation=ServicingOperation.REQUEST_PAYMENT,
                amount=Decimal(payment["amount"]),
            ),
        )
        assert decision.decision.value == "allow", f"history payment denied: {decision.reason}"
        history_ids.add(request_id)
    return history_ids


def _block_type(block: Any) -> str | None:
    return block.get("type") if isinstance(block, dict) else getattr(block, "type", None)


def trace_turn(trace: list[dict], text: str, new_messages: list[dict], reply: str) -> list[Any]:
    """Append one caller turn to the trace. Returns the turn's tool_use blocks."""
    trace.append({"role": "user", "content": text})
    tool_uses = []
    thinking: list[str] = []

    def add(entry: dict) -> None:
        if thinking:
            entry["thinking"] = "\n".join(thinking)
            thinking.clear()
        trace.append(entry)

    for message in new_messages:
        if message["role"] == "user":
            if isinstance(message["content"], list):
                for block in message["content"]:
                    trace.append({"role": "tool_result", "content": block["content"]})
            continue  # the caller's own text is already in the trace
        blocks = message["content"]
        has_tools = any(_block_type(b) == "tool_use" for b in blocks)
        for block in blocks:
            kind = _block_type(block)
            if kind == "thinking" and getattr(block, "thinking", ""):
                thinking.append(block.thinking)
            elif kind == "text" and has_tools and block.text.strip():
                add({"role": "assistant", "content": block.text})
            elif kind == "tool_use":
                tool_uses.append(block)
                add({"role": "tool_call", "name": block.name, "content": json.dumps(block.input, indent=2)})

    # What the caller actually heard; the loop's fixed replies are not in
    # the message history.
    add({"role": "assistant", "content": reply})
    return tool_uses


def end_state(db: Session, session: Any, history_ids: set[str], tool_calls: list[dict]) -> dict:
    records = [
        {
            "request_id": r.request_id,
            "operation": r.operation,
            "account_id": r.account_id,
            "amount": str(r.amount) if r.amount is not None else None,
            "decision": r.decision,
            "reason": r.reason,
        }
        for r in db.scalars(select(models.ServicingRequestRecord)).all()
        if r.request_id not in history_ids
    ]
    turns = db.scalars(
        select(models.ConversationEventRecord)
        .where(models.ConversationEventRecord.event_type == "turn_completed")
        .order_by(models.ConversationEventRecord.event_id)
    ).all()
    handoff = session.state.handoff
    return {
        "records": records,
        "handoff": handoff.reason if handoff is not None else None,
        "turn_outcomes": [t.outcome for t in turns],
        "turn_latencies_s": [round(t.latency_ms / 1000, 3) for t in turns],
        "tool_calls": tool_calls,
    }


@dataclass
class Settings:
    agent: str
    judge: bool
    model: str
    effort: str
    timeout_s: float
    anthropic_client: anthropic.Anthropic | None


def make_llm(settings: Settings, case_id: str) -> Any:
    if settings.agent == "oracle":
        return oracle_llm(case_id)
    if settings.agent in CONSTANT_REPLIES:
        return ConstantLLM(CONSTANT_REPLIES[settings.agent])
    # The production client class; retries happen in RecordingLLM, where
    # they are counted.
    return ClaudeClient(settings.anthropic_client, model=settings.model, effort=settings.effort)


def run_case(case: dict, rep: int, world: dict, settings: Settings) -> tuple[dict, list[dict]]:
    """Play and grade one case. Returns (results row, trace)."""
    db = new_database()
    try:
        history_ids = seed(db, world, case)
        llm = RecordingLLM(
            make_llm(settings, case["id"]),
            expected_model=settings.model if settings.agent == "real" else None,
            deadline=time.monotonic() + settings.timeout_s,
        )
        session = SessionStore().create(case["customer"])
        trace: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        tool_calls: list[dict] = []

        for turn_number, text in enumerate(case["turns"], start=1):
            before = len(session.messages)
            try:
                result = run_turn(session, text, llm=llm, db=db, system=SYSTEM_PROMPT)
            except AssertionError as exc:  # a scripted model ran out of steps
                raise HarnessError("script_exhausted", str(exc), calls=llm.calls) from exc
            if llm.gave_up:
                # The conversation is spoiled; don't pay for its later turns.
                raise HarnessError("api_unavailable", f"model unavailable in turn {turn_number}",
                                   calls=llm.calls)
            tool_uses = trace_turn(trace, text, session.messages[before:], result.reply)
            for index, call in enumerate(result.tool_calls):
                tool_calls.append({
                    "turn": turn_number,
                    "name": call.name,
                    "ok": call.ok,
                    "error": call.error,
                    # The loop's own forced handoff has no tool_use block.
                    "args": tool_uses[index].input if index < len(tool_uses) else None,
                })

        state = end_state(db, session, history_ids, tool_calls)
    finally:
        engine = db.get_bind()
        db.close()
        engine.dispose()

    grade, explanation, _ = grading.grade_programmatic(case, state)
    stop_reasons = [c["stop_reason"] for c in llm.calls]
    truncated = "max_tokens" in stop_reasons

    row: dict[str, Any] = {
        "prompt_id": case["id"],
        "rep": rep,
        "prompt": "\n".join(f"Caller: {t}" for t in case["turns"]),
        "tags": case["tags"],
        "status": "truncated" if truncated else "ok",
        "stop_reason": stop_reasons[-1] if stop_reasons else None,
        "model": Counter(c["model"] for c in llm.calls).most_common(1)[0][0] if llm.calls else None,
        "usage": _sum_usage(c["usage"] for c in llm.calls),
        "latency_s": round(sum(state["turn_latencies_s"]) / len(state["turn_latencies_s"]), 3),
        "turn_max_s": max(state["turn_latencies_s"]),
        "tool_calls": len(tool_calls),
        "refusals": stop_reasons.count("refusal"),
        "meta": {
            "end_state": state,
            "model_calls": len(llm.calls),
            "retries": llm.retries,
            "calls": llm.calls,
        },
    }

    if settings.judge:
        try:
            judged = grading.run_judge(settings.anthropic_client, trace, case["judge_notes"])
        except grading.JudgeError as exc:
            raise HarnessError("grader_error", str(exc), calls=llm.calls) from exc
        grade["quality"], explanation["quality"] = grading.grade_quality(judged["verdicts"])
        row["judge_model"] = judged["model"]
        row["judge_usage"] = judged["usage"]
        row["meta"]["judge_verdicts"] = judged["verdicts"]
        # Only with the judge: without quality it would mean something else.
        grade["all_pass"], explanation["all_pass"] = grading.all_pass(grade)

    # Headline metric first; the report treats the first binary metric as it.
    order = ["all_pass", "outcome", "compliance", "quality", "payments", "handoff"]
    row["grade"] = {k: grade[k] for k in order if k in grade}
    row["explanation"] = {k: explanation[k] for k in order if k in explanation}
    return row, trace


def _sum_usage(usages: Any) -> dict[str, int]:
    total: Counter = Counter()
    for usage in usages:
        total.update(usage)
    return {
        key: total[key]
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    }


# --- harness gate -----------------------------------------------------------


def harness_sha(state: dict) -> tuple[str, list[str]]:
    paths = [Path(__file__).resolve()] + [REPO_ROOT / p for p in state.get("harness_paths", [])]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(REPO_ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest(), [str(p.relative_to(REPO_ROOT)) for p in paths]


def check_harness(state_path: Path, approve: bool) -> None:
    """Refuse a paid run on a harness no person has approved since it last
    changed. --approve-harness is for the person running the eval."""
    state = json.loads(state_path.read_text())
    sha, files = harness_sha(state)
    if state.get("harness_sha") == sha:
        return
    if approve:
        state["harness_sha"] = sha
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        print(f"harness approved: sha256 {sha[:12]} over {', '.join(files)}", file=sys.stderr)
        return
    if state.get("harness_sha") is None:
        print(f"No approved harness yet (sha256 {sha[:12]} over {', '.join(files)}).", file=sys.stderr)
    else:
        print(f"Harness changed since it was approved ({state['harness_sha'][:12]} -> {sha[:12]}).", file=sys.stderr)
    print("Review it, then run once with --approve-harness.", file=sys.stderr)
    sys.exit(2)


# --- run --------------------------------------------------------------------


def wilson(passed: float, n: int) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    z = 1.96
    p = passed / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - spread), min(1.0, centre + spread)


def cost_usd(model: str | None, usage: dict | None) -> float:
    if not model or not usage:
        return 0.0
    price = next((p for name, p in PRICES.items() if model.startswith(name)), None)
    if price is None:
        raise ValueError(f"no price for model {model}")
    per_token_in = price["in"] / 1_000_000
    return (
        usage["input_tokens"] * per_token_in
        + usage["cache_creation_input_tokens"] * per_token_in * 1.25
        + usage["cache_read_input_tokens"] * per_token_in * 0.1
        + usage["output_tokens"] * price["out"] / 1_000_000
    )


def summarise(rows: list[dict], errors: list[dict], cases: dict[str, dict], wall_s: float) -> str:
    lines = []
    scored = [r for r in rows if r["status"] == "ok"]
    # A later success supersedes an attempt's error; its cost still counts.
    succeeded = {(r["prompt_id"], r["rep"]) for r in rows}
    unresolved = {(e["prompt_id"], e["rep"]) for e in errors} - succeeded
    lines.append(
        f"{len(rows)} rows ({len(scored)} scored, {len(rows) - len(scored)} truncated), "
        f"{len(errors)} errored attempts ({len(unresolved)} still unscored), {wall_s:.0f}s wall-clock"
    )
    for metric in ("all_pass", "outcome", "compliance", "quality", "payments", "handoff"):
        values = [r["grade"][metric] for r in scored if metric in r["grade"]]
        if not values:
            continue
        low, high = wilson(sum(values), len(values))
        lines.append(
            f"  {metric:11} {sum(values) / len(values):6.1%}  "
            f"({int(sum(values))}/{len(values)}, 95% CI {low:.0%}-{high:.0%})"
        )

    # Confusion matrices: did the agent act when it should, and only then?
    for label, should_act, acted in (
        ("handoff", lambda c: isinstance(c["expect"]["handoff"], list),
         lambda r: r["meta"]["end_state"]["handoff"] is not None),
        ("payment", lambda c: bool(c["expect"]["payments"]),
         lambda r: any(x["operation"] == "request_payment" for x in r["meta"]["end_state"]["records"])),
    ):
        tp = fp = fn = tn = 0
        for r in scored:
            case = cases[r["prompt_id"]]
            if label == "handoff" and case["expect"]["handoff"] == "any_or_none":
                continue
            want, did = should_act(case), acted(r)
            tp += want and did
            fn += want and not did
            fp += did and not want
            tn += not want and not did
        if not tp + fp + fn + tn:
            continue
        recall = f"{tp / (tp + fn):.0%}" if tp + fn else "-"
        specificity = f"{tn / (tn + fp):.0%}" if tn + fp else "-"
        lines.append(
            f"  {label} acted: recall {recall} ({tp}/{tp + fn} when needed), "
            f"specificity {specificity} ({tn}/{tn + fp} held back when not)"
        )

    agent_cost = sum(cost_usd(r.get("model"), r.get("usage")) for r in rows)
    judge_cost = sum(cost_usd(r.get("judge_model"), r.get("judge_usage")) for r in rows)
    error_cost = sum(
        cost_usd(call["model"], call["usage"]) for e in errors for call in e.get("calls", [])
    )
    if agent_cost or judge_cost or error_cost:
        lines.append(
            f"  cost: agent ${agent_cost:.4f} + judge ${judge_cost:.4f} "
            f"+ errored attempts ${error_cost:.4f} = ${agent_cost + judge_cost + error_cost:.4f}"
        )
    if scored:
        latencies = sorted(r["latency_s"] for r in scored)
        lines.append(
            f"  latency per caller turn (case means): median {latencies[len(latencies) // 2]:.1f}s, "
            f"max {latencies[-1]:.1f}s; slowest single turn {max(r['turn_max_s'] for r in scored):.1f}s"
        )
    return "\n".join(lines)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", choices=["real", "oracle", *CONSTANT_REPLIES], default="real",
                        help="real: the agent on Claude; others are scripted stand-ins")
    parser.add_argument("--judge", choices=["on", "off"], default=None,
                        help="default: on for --agent real, off otherwise")
    parser.add_argument("--variant", default="baseline")
    parser.add_argument("--out", type=Path, help="output directory (required unless --agent real)")
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout-s", type=float, default=300)
    parser.add_argument("--model", default=config.AGENT_MODEL)
    parser.add_argument("--approve-harness", action="store_true")
    args = parser.parse_args(argv)

    judge = (args.judge or ("on" if args.agent == "real" else "off")) == "on"
    paid = args.agent == "real" or judge

    if paid and config.AGENT_LLM != "anthropic":
        print("Refusing to make paid calls: set COLLECTWISE_AGENT_LLM=anthropic.", file=sys.stderr)
        return 1
    if args.agent != "real" and args.out is None:
        print("Scripted agents need --out, so they never mix with real results.", file=sys.stderr)
        return 1
    if paid:
        check_harness(FLOW_DIR / "_state.json", args.approve_harness)

    data = json.loads(CASES_PATH.read_text())
    cases = {case["id"]: case for case in data["cases"]}
    selected = list(cases) if not args.cases else args.cases.split(",")
    unknown = [c for c in selected if c not in cases]
    if unknown:
        print(f"Unknown case ids: {', '.join(unknown)}", file=sys.stderr)
        return 1

    out = args.out or FLOW_DIR / args.variant
    (out / "traces").mkdir(parents=True, exist_ok=True)
    results_path, errors_path = out / "results.jsonl", out / "errors.jsonl"
    done = {(r["prompt_id"], r["rep"]) for r in _read_jsonl(results_path)}
    todo = [(cid, rep) for cid in selected for rep in range(args.reps) if (cid, rep) not in done]

    settings = Settings(
        agent=args.agent,
        judge=judge,
        model=args.model,
        effort=config.AGENT_EFFORT,
        timeout_s=args.timeout_s,
        # Retries are done (and counted) by RecordingLLM and the judge.
        anthropic_client=anthropic.Anthropic(max_retries=0, timeout=120) if paid else None,
    )
    write_lock = threading.Lock()
    print(
        f"{len(todo)} attempts to run ({len(done)} already done) | agent={args.agent} "
        f"model={args.model if args.agent == 'real' else '-'} judge={grading.JUDGE_MODEL if judge else 'off'} "
        f"-> {out}",
        file=sys.stderr,
    )

    def attempt(item: tuple[str, int]) -> None:
        case_id, rep = item
        try:
            row, trace = run_case(cases[case_id], rep, data["world"], settings)
        except Exception as exc:
            # Includes our own bugs and non-transient API errors: recorded,
            # printed, and never scored as a model failure.
            if not isinstance(exc, HarnessError):
                logging.getLogger(__name__).exception("%s rep%d crashed", case_id, rep)
                exc = HarnessError("harness_crash", f"{type(exc).__name__}: {exc}")
            entry = {
                "prompt_id": case_id,
                "rep": rep,
                "failure_class": exc.failure_class,
                "detail": exc.detail,
                "calls": exc.calls,
                "at": datetime.now(timezone.utc).isoformat(),
            }
            with write_lock, errors_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
            print(f"  ERROR {case_id} rep{rep}: {exc.failure_class}: {exc.detail}", file=sys.stderr)
            return
        (out / "traces" / f"{case_id}_rep{rep}.json").write_text(json.dumps(trace, indent=2))
        with write_lock, results_path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        failed = [k for k, v in row["grade"].items() if v < 1]
        print(f"  {case_id} rep{rep}: {'pass' if not failed else 'FAIL ' + ','.join(failed)}", file=sys.stderr)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(attempt, todo))

    rows = [r for r in _read_jsonl(results_path) if r["prompt_id"] in selected]
    errors = [e for e in _read_jsonl(errors_path) if e["prompt_id"] in selected]
    print(summarise(rows, errors, cases, time.monotonic() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
