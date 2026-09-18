"""Read-only MCP client for Robinhood's agentic trading server.

Phase 1: read-only. Trade/mutate tools are classified and REFUSED by the
policy gate below — they are never called, even if the user asks.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from rh_auth import MCP_URL, build_provider, make_http_client

# --------------------------------------------------------------------------- #
# Tool policy (phase 1: read-only)
# Tool names confirmed against the live server by slijeff/robinhood-rest2mcp.
# --------------------------------------------------------------------------- #
TRADING_TOOLS = frozenset(
    {
        "place_equity_order",
        "place_option_order",
        "cancel_equity_order",
        "cancel_option_order",
        "exercise_option",
        "cancel_option_exercise",
    }
)
MUTATING_PREFIXES = (
    "create_",
    "update_",
    "add_",
    "remove_",
    "follow_",
    "unfollow_",
    "delete_",
)
READ_ONLY_TOOLS = frozenset(
    {
        "search",
        "run_scan",
        "review_equity_order",  # non-binding price preview
        "review_option_order",  # non-binding price preview
    }
)


def classify_tool(name: str) -> str:
    """read | trade | mutate. Unknown verbs are treated as mutate (deny)."""
    n = name.lower()
    if n in TRADING_TOOLS:
        return "trade"
    if n in READ_ONLY_TOOLS or n.startswith("get_"):
        return "read"
    if n.startswith(MUTATING_PREFIXES):
        return "mutate"
    return "mutate"  # unknown: fail closed, like the reference implementation


def check_call_allowed(name: str) -> None:
    kind = classify_tool(name)
    if kind == "read":
        return
    if kind == "trade":
        raise PermissionError(
            f"REFUSED: '{name}' is a TRADE tool. Phase 1 is read-only; "
            "order placement is not wired up."
        )
    raise PermissionError(
        f"REFUSED: '{name}' is classified '{kind}' (not read-only). "
        "Phase 1 only calls read tools."
    )


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def mcp_session(port: int = 8765, *, interactive: bool = True, auth_flow=None):
    """Yield an initialized ClientSession with OAuth attached.

    On first use (no tokens in memory) the provider performs the 401 ->
    discovery -> dynamic registration -> browser redirect flow using the
    handlers from rh_auth. In non-interactive mode it raises with
    instructions instead.
    """
    provider, storage = build_provider(port, interactive=interactive, flow=auth_flow)
    http_client = make_http_client(auth=provider, timeout=60)
    async with http_client:
        async with streamable_http_client(MCP_URL, http_client=http_client) as (
            read_stream,
            write_stream,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def list_tools(port: int = 8765, *, interactive: bool = True, auth_flow=None) -> list[dict]:
    """Initialize + tools/list; return inventory with policy classification."""
    async with mcp_session(port, interactive=interactive, auth_flow=auth_flow) as session:
        result = await session.list_tools()
    inventory = []
    for t in result.tools:
        schema = t.input_schema or {}
        inventory.append(
            {
                "name": t.name,
                "description": t.description or "",
                "access": classify_tool(t.name),
                "required_args": schema.get("required", []),
                "properties": sorted((schema.get("properties") or {}).keys()),
            }
        )
    return inventory


async def call_read_tool(
    name: str, args: dict, port: int = 8765, *, interactive: bool = True
) -> dict:
    """Call a single tool after the read-only policy gate."""
    check_call_allowed(name)
    async with mcp_session(port, interactive=interactive) as session:
        result = await session.call_tool(name, args)
    out = {"is_error": bool(result.is_error), "content": []}
    for block in result.content or []:
        btype = getattr(block, "type", "?")
        if btype == "text":
            out["content"].append({"type": "text", "text": block.text})
        else:
            out["content"].append({"type": btype, "repr": repr(block)[:500]})
    return out


async def call_tool_raw(name: str, args: dict, port: int = 8765) -> dict:
    """Call any tool WITHOUT the read-only policy gate.

    Only rh_trade.place_staged may use this, after its own account,
    defined-risk, and review gates pass. Never expose via the `call` CLI.
    """
    async with mcp_session(port, interactive=False) as session:
        result = await session.call_tool(name, args)
    out = {"is_error": bool(result.is_error), "content": []}
    for block in result.content or []:
        btype = getattr(block, "type", "?")
        if btype == "text":
            out["content"].append({"type": "text", "text": block.text})
        else:
            out["content"].append({"type": btype, "repr": repr(block)[:500]})
    return out


def print_inventory(inventory: list[dict]) -> None:
    by_access: dict[str, list[dict]] = {}
    for t in inventory:
        by_access.setdefault(t["access"], []).append(t)
    for access in ("read", "trade", "mutate"):
        tools = by_access.get(access, [])
        print(f"\n## {access.upper()} ({len(tools)})")
        for t in tools:
            req = ", ".join(t["required_args"]) or "no required args"
            print(f"  - {t['name']}: {t['description'][:110]}")
            print(f"    args: {req}")
    print()
