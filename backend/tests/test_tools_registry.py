import json
import logging
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.db import models
from backend.app.tools.base import ToolContext, ToolResult
from backend.app.tools.registry import TOOLS, execute_tool, tool_schemas
from backend.tests.helpers import context_for, seed_customer


@pytest.fixture
def ctx(db_session: Session) -> ToolContext:
    seed_customer(db_session, "cust_a", [("acct_a_1", "USD", "active")])
    return context_for(db_session, "cust_a")


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model))


def property_names(schema: Any) -> set[str]:
    """Every property name anywhere in a JSON schema, at any depth."""
    names: set[str] = set()
    if isinstance(schema, dict):
        names.update(schema.get("properties", {}).keys())
        for value in schema.values():
            names |= property_names(value)
    elif isinstance(schema, list):
        for item in schema:
            names |= property_names(item)
    return names


def test_registry_lists_every_tool() -> None:
    assert set(TOOLS) == {
        "list_accounts",
        "get_account_summary",
        "propose_payment",
        "confirm_payment",
        "cancel_payment",
        "get_request_status",
        "handoff_to_human",
    }


def test_no_tool_schema_lets_the_llm_supply_an_identity() -> None:
    for tool in tool_schemas():
        names = property_names(tool["input_schema"])
        assert not any("customer" in name for name in names), tool["name"]


def test_every_tool_rejects_unknown_arguments() -> None:
    for spec in TOOLS.values():
        assert spec.args_model.model_config.get("extra") == "forbid", spec.name


def test_tool_schemas_are_json_and_described() -> None:
    schemas = tool_schemas()

    json.dumps(schemas)
    for tool in schemas:
        assert tool["description"]
        for prop in tool["input_schema"].get("properties", {}).values():
            assert prop.get("description"), tool["name"]


def test_unknown_tool(ctx: ToolContext) -> None:
    assert execute_tool(ctx, "transfer_money", {}) == ToolResult.failure("unknown_tool")


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_tool_is_refused_after_handoff(ctx: ToolContext, name: str) -> None:
    execute_tool(ctx, "handoff_to_human", {"reason": "caller_requested"})

    result = execute_tool(ctx, name, {})

    assert result == ToolResult.failure("conversation_handed_off")
    assert count(ctx.db, models.ServicingRequestRecord) == 0


@pytest.mark.parametrize("amount", [200, 200.1, "200.10", 0.5])
def test_llm_style_amounts_are_accepted(ctx: ToolContext, amount: Any) -> None:
    result = execute_tool(
        ctx,
        "propose_payment",
        {"account_id": "acct_a_1", "amount": amount},
    )

    assert result.ok is True
    assert Decimal(result.data["amount"]) == Decimal(str(amount))


@pytest.mark.parametrize(
    ("raw_args", "fields"),
    [
        ({"account_id": "acct_a_1", "amount": 10.001}, ["amount"]),
        ({"amount": "10.00"}, ["account_id"]),
        ({"account_id": "acct_a_1", "amount": "10.00", "customer_id": "cust_b"}, ["customer_id"]),
        ("pay 10 dollars", ["arguments"]),
    ],
)
def test_invalid_arguments_name_fields_but_not_values(
    ctx: ToolContext,
    raw_args: Any,
    fields: list[str],
) -> None:
    result = execute_tool(ctx, "propose_payment", raw_args)

    assert result == ToolResult.failure("invalid_arguments", fields=fields)
    assert "cust_b" not in json.dumps(result.data)
    assert ctx.state.pending_payment is None


def test_unexpected_tool_error_is_contained_and_rolled_back(
    ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_tool(tool_ctx: ToolContext, args: Any) -> ToolResult:
        tool_ctx.db.add(models.CustomerRecord(customer_id="cust_half", display_name="x"))
        tool_ctx.db.flush()
        raise RuntimeError("database exploded with secret details")

    monkeypatch.setitem(
        TOOLS,
        "list_accounts",
        replace(TOOLS["list_accounts"], function=broken_tool),
    )

    with caplog.at_level(logging.ERROR):
        result = execute_tool(ctx, "list_accounts", {})

    assert result == ToolResult.failure("internal_error")
    assert "secret" not in json.dumps(result.data)
    # The half-finished insert was rolled back and the session still works.
    assert ctx.db.get(models.CustomerRecord, "cust_half") is None
    assert ctx.db.get(models.CustomerRecord, "cust_a") is not None
    assert "list_accounts" in caplog.text


def test_scripted_conversation_through_the_dispatcher(ctx: ToolContext) -> None:
    # The shape of what the M3 agent loop will do, with a scripted "LLM".
    def call(name: str, args: dict | None = None) -> ToolResult:
        return execute_tool(ctx, name, args or {})

    # Turn 1: "Pay 200 from my account."
    ctx.state.start_caller_turn()
    accounts = call("list_accounts")
    account_id = accounts.data["accounts"][0]["account_id"]
    proposal = call("propose_payment", {"account_id": account_id, "amount": 200})
    assert proposal.data["requires_confirmation"] is True
    # The model gets ahead of itself and confirms before the caller answers.
    assert call("confirm_payment") == ToolResult.failure("confirmation_required")

    # Turn 2: "Yes."
    ctx.state.start_caller_turn()
    confirmation = call("confirm_payment")
    assert confirmation.data["status"] == "recorded"

    # Turn 3: "Did that go through?"
    ctx.state.start_caller_turn()
    status = call("get_request_status", {"request_id": confirmation.data["request_id"]})
    assert status.data["decision"] == "allow"
    assert status.data["amount"] == "200.00"

    # Turn 4: "Let me talk to a person."
    ctx.state.start_caller_turn()
    assert call("handoff_to_human", {"reason": "caller_requested"}).ok is True
    assert call("list_accounts") == ToolResult.failure("conversation_handed_off")

    assert count(ctx.db, models.ServicingRequestRecord) == 1
