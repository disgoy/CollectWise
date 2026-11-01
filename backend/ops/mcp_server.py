"""A read-only operations MCP server for staff (M6.6).

Lets an assistant such as Claude Desktop or Claude Code answer "how fast were
live calls this hour?", "did anything fail?", "how did the last eval do?"
from CollectWise's own numbers. Its own process over stdio: no network
listener, never imported by the app, never offered to the agent. It opens the
database read-only, so even a bug here can't write, and every result is
checked for anything that could be someone's words before it is returned.

Usage, from the repo root (an MCP client starts it):
    python -m backend.ops.mcp_server
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from backend.app import config
from backend.app.services.evidence import check_private
from backend.ops import queries

DOCS = Path(__file__).resolve().parents[2] / "docs"
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
Minutes = Annotated[int, Field(ge=1, le=7 * 24 * 60, description="How far back to look, in minutes (up to 7 days)")]
Limit = Annotated[int, Field(ge=1, le=100, description="How many calls, newest first (up to 100)")]


def read_only_engine(url: str) -> Engine:
    """An engine that can't write, whatever the code using it does."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if not parsed.database or parsed.database == ":memory:":
            raise ValueError("The operations server needs a database file, not an in-memory database")
        path = Path(parsed.database).resolve()
        return create_engine(f"sqlite:///file:{path}?mode=ro&uri=true")
    return create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})


_engine: Engine | None = None


def use_engine(engine: Engine | None) -> None:
    """For tests: read through this engine instead of the configured database."""
    global _engine
    _engine = engine


def _session():
    global _engine
    if _engine is None:
        _engine = read_only_engine(config.DATABASE_URL)
    return sessionmaker(bind=_engine)()


def _checked(result):
    check_private({"ops": result})
    return result


def _now() -> datetime:
    return datetime.now(timezone.utc)


server = MCPServer(
    name="collectwise-ops",
    instructions=(
        "Read-only numbers about the CollectWise voice servicing agent: live-call speed, recent calls, "
        "conversation outcomes and errors, eval results and database health. Counts, codes, timings and "
        "short call references only: no customer ids, nothing callers said, no amounts."
    ),
)


@server.tool(annotations=READ_ONLY, structured_output=True)
def service_health() -> dict[str, Any]:
    """Whether the database answers and its schema is at the latest migration."""
    with _session() as db:
        return _checked(queries.service_health(db))


@server.tool(annotations=READ_ONLY, structured_output=True)
def live_call_speed(minutes: Minutes = 60) -> dict[str, Any]:
    """Live-call speed over a window: calls, turns, outcomes, time to the agent's first words and first
    audio after the caller finished (p50/p95/max, milliseconds), model and tool times, word confidence."""
    with _session() as db:
        return _checked(queries.live_call_speed(db, minutes=minutes, now=_now()))


@server.tool(annotations=READ_ONLY, structured_output=True)
def recent_calls(limit: Limit = 10) -> list[dict[str, Any]]:
    """The latest live calls: a short reference, start time, turns, outcomes, median first audio and any
    replies that were text only."""
    with _session() as db:
        return _checked(queries.recent_calls(db, limit=limit))


@server.tool(annotations=READ_ONLY, structured_output=True)
def conversation_outcomes(minutes: Minutes = 60) -> dict[str, Any]:
    """What happened in conversations over a window: turn outcomes, model calls that failed, tool errors by
    code, payment requests cancelled by reason, handoffs to a person."""
    with _session() as db:
        return _checked(queries.conversation_outcomes(db, minutes=minutes, now=_now()))


@server.tool(annotations=READ_ONLY, structured_output=True)
def eval_results() -> dict[str, Any]:
    """The agent eval: each version's pass rates and the judge checks it failed, from this machine's results."""
    return _checked(queries.eval_results())


def _doc(name: str) -> str:
    return (DOCS / name).read_text()


@server.resource("collectwise://docs/failure-modes", name="Failure modes", mime_type="text/markdown")
def failure_modes() -> str:
    """What the caller hears, what is recorded and what is left when each part fails."""
    return _doc("FAILURE_MODES.md")


@server.resource("collectwise://docs/data-handling", name="Data handling", mime_type="text/markdown")
def data_handling() -> str:
    """Where data lives, what it never holds, and how long it is kept."""
    return _doc("DATA_HANDLING.md")


@server.resource("collectwise://docs/load-test", name="Load test", mime_type="text/markdown")
def load_test() -> str:
    """How many calls one server takes, and where it slows down."""
    return _doc("LOAD_TEST.md")


if __name__ == "__main__":
    server.run("stdio")
