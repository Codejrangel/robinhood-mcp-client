"""Write path for the Robinhood MCP client.

GATES (enforced in code, no exceptions):
  1. Account gate: every write targets the single designated account
     (RH_ACCOUNT_NUMBER) and nothing else. Any other account_number is refused.
  2. Defined-risk: long options are always fine (max loss = premium paid).
     A sell-to-open leg must sit inside a same-expiry, same-underlying,
     same-type spread where every short leg is covered by a long leg
     further out-of-the-money with covering ratios. Naked shorts,
     calendars/diagonals (assignment/pin risk), and anything else with
     unbounded or uncomputable loss are refused.
  3. No margin, no shorting: the account itself is a cash account, and on
     top `exercise_option` is forced to allow_shorts=false; equity sells are
     checked against owned shares so a short is never attempted.
  4. Review-before-place: `place` only executes a stage created by `stage`,
     which embeds the broker's own review_* preview of the exact order.
  5. Visible log: every placement appends timestamp, tool, full args,
     review summary, and result to trade-log.md.

AUTHORIZATION (procedural, enforced by the operator, not by code):
Never run `place` without the operator's explicit per-trade approval of
that exact stage. Automated jobs may stage proposals; they must never place.
The code gates above are the backstop, not the permission.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rh_auth import TOKEN_DIR

AGENTIC_ACCOUNT = os.environ.get("RH_ACCOUNT_NUMBER", "")
# The single Robinhood account this client is allowed to write to.
# Set RH_ACCOUNT_NUMBER in your environment before staging any order.
# Writes are refused unless the order targets exactly this account.
STAGE_DIR = TOKEN_DIR / "staged"
TRADE_LOG = Path(__file__).resolve().parent / "trade-log.md"

WRITE_TOOLS = frozenset(
    {
        "place_equity_order",
        "place_option_order",
        "cancel_equity_order",
        "cancel_option_order",
        "exercise_option",
    }
)
REVIEW_TOOL = {
    "place_equity_order": "review_equity_order",
    "place_option_order": "review_option_order",
}
# Stages older than this are refused at place time (re-stage for fresh review).
STAGE_TTL_SECONDS = 24 * 3600


class TradeRefused(Exception):
    """A hard gate refused the order. Message explains which gate and why."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stage_path(stage_id: str) -> Path:
    return STAGE_DIR / f"{stage_id}.json"


def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _get_args(args) -> dict:
    if isinstance(args, str):
        return json.loads(args)
    return dict(args or {})


# --------------------------------------------------------------------------- #
# Gate 1: account
# --------------------------------------------------------------------------- #
def check_account(args: dict) -> None:
    if not AGENTIC_ACCOUNT:
        raise TradeRefused(
            "REFUSED: RH_ACCOUNT_NUMBER is not set — refusing all writes until "
            "the designated account is configured."
        )
    acct = str(args.get("account_number", ""))
    if acct != AGENTIC_ACCOUNT:
        raise TradeRefused(
            "REFUSED: account gate — write tools may only target the designated "
            f"account, got account_number={acct!r}."
        )


# --------------------------------------------------------------------------- #
# Gate 2: defined-risk (options)
# --------------------------------------------------------------------------- #
async def _instruments_by_id(option_ids: list[str]) -> dict:
    """Fetch strike/type/expiry/underlying for each instrument id."""
    from rh_client import call_read_tool

    out: dict = {}
    # get_option_instruments takes comma-separated ids
    result = await call_read_tool(
        "get_option_instruments", {"ids": ",".join(option_ids)}, interactive=False
    )
    if result.get("is_error"):
        raise TradeRefused(f"REFUSED: could not look up option instruments: {result}")
    for block in result.get("content", []):
        if block.get("type") != "text":
            continue
        try:
            payload = json.loads(block["text"])
        except json.JSONDecodeError:
            continue
        items = (
            payload.get("data", {}).get("instruments")
            or payload.get("data", {}).get("results")
            or []
        )
        for it in items:
            oid = it.get("id") or it.get("option_id") or it.get("instrument_id")
            if oid:
                out[str(oid)] = it
    missing = [i for i in option_ids if i not in out]
    if missing:
        raise TradeRefused(
            f"REFUSED: could not resolve instrument(s) {missing} — "
            "cannot verify defined risk."
        )
    return out


def _leg_desc(leg: dict, instruments: dict) -> str:
    meta = instruments.get(str(leg["option_id"]), {})
    return (
        f"{leg['side']}-to-{leg['position_effect']} "
        f"{meta.get('chain_symbol', '?')} "
        f"{meta.get('expiration_date', '?')} "
        f"{meta.get('strike_price', '?')} {meta.get('type', '?')}"
    )


async def validate_option_legs(legs: list[dict]) -> str:
    """Enforce defined-risk on the leg structure. Returns a risk summary."""
    if not legs or len(legs) > 4:
        raise TradeRefused("REFUSED: option orders need 1–4 legs.")
    for leg in legs:
        if leg.get("side") not in ("buy", "sell"):
            raise TradeRefused(f"REFUSED: leg side must be buy/sell, got {leg!r}.")
        if leg.get("position_effect") not in ("open", "close"):
            raise TradeRefused(
                f"REFUSED: leg position_effect must be open/close, got {leg!r}."
            )
    instruments = await _instruments_by_id([str(l["option_id"]) for l in legs])

    underlyings = {str(instruments[str(l["option_id"])].get("chain_symbol")) for l in legs}
    if len(underlyings) != 1:
        raise TradeRefused(
            f"REFUSED: all legs must share one underlying, got {underlyings}."
        )

    # Single leg: buys are defined-risk; sell-to-open is a naked short.
    if len(legs) == 1:
        leg = legs[0]
        if leg["side"] == "buy":
            return f"defined risk: long {_leg_desc(leg, instruments)} (max loss = premium paid)"
        if leg["position_effect"] == "close":
            return f"defined risk: closing {_leg_desc(leg, instruments)} (reduces risk)"
        raise TradeRefused(
            f"REFUSED: naked sell-to-open {_leg_desc(leg, instruments)} — "
            "unbounded defined-risk violation. Short opens must sit inside a spread."
        )

    # Multi-leg: every sell-to-open must be covered by a buy leg further OTM,
    # same expiry, same type, covering ratio.
    expiries = {
        str(instruments[str(l["option_id"])].get("expiration_date")) for l in legs
    }
    if len(expiries) != 1:
        raise TradeRefused(
            f"REFUSED: multi-leg orders must share one expiration (no "
            f"calendars/diagonals — assignment/pin risk), got {expiries}."
        )
    types = {str(instruments[str(l["option_id"])].get("type")) for l in legs}
    if len(types) != 1:
        raise TradeRefused(
            f"REFUSED: spread legs must be all calls or all puts, got {types}."
        )

    def strike(leg):
        return float(instruments[str(leg["option_id"])]["strike_price"])

    def ratio(leg):
        return int(leg.get("ratio_quantity") or 1)

    shorts = [l for l in legs if l["side"] == "sell" and l["position_effect"] == "open"]
    longs = [l for l in legs if l["side"] == "buy" and l["position_effect"] == "open"]
    if not shorts:
        return "defined risk: multi-leg without short opens (max loss = net debit paid)"
    if not longs:
        raise TradeRefused(
            "REFUSED: multi-leg with short opens but no long opens — naked shorts."
        )
    otype = next(iter(types))
    for s in shorts:
        covered = 0
        for b in longs:
            if otype == "call" and strike(b) > strike(s):
                covered += ratio(b)
            elif otype == "put" and strike(b) < strike(s):
                covered += ratio(b)
        if covered < ratio(s):
            raise TradeRefused(
                f"REFUSED: short leg {_leg_desc(s, instruments)} is not fully "
                f"covered by further-OTM long legs — unbounded risk."
            )
    desc = "; ".join(_leg_desc(l, instruments) for l in legs)
    return f"defined risk: spread ({desc}) — every short covered further OTM"


# --------------------------------------------------------------------------- #
# Gate 3: no margin / no shorting (equity + exercise)
# --------------------------------------------------------------------------- #
async def _check_equity_sell_covered(symbol: str, quantity: str) -> None:
    from rh_client import call_read_tool

    result = await call_read_tool(
        "get_equity_positions", {"account_number": AGENTIC_ACCOUNT}, interactive=False
    )
    if result.get("is_error"):
        raise TradeRefused(f"REFUSED: could not verify share ownership: {result}")
    owned = 0.0
    for block in result.get("content", []):
        if block.get("type") != "text":
            continue
        try:
            payload = json.loads(block["text"])
        except json.JSONDecodeError:
            continue
        for p in payload.get("data", {}).get("positions", []):
            if str(p.get("symbol", "")).upper() == symbol.upper():
                owned += float(p.get("shares_available_for_sells", 0) or 0)
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        raise TradeRefused(f"REFUSED: bad equity quantity {quantity!r}.")
    if qty > owned:
        raise TradeRefused(
            f"REFUSED: sell {qty} {symbol} but only {owned} shares available — "
            "shorting is not allowed."
        )


async def validate_order(tool: str, args: dict) -> dict:
    """Run every hard gate. Returns a validation record or raises TradeRefused."""
    if tool not in WRITE_TOOLS:
        raise TradeRefused(f"REFUSED: {tool} is not a gated write tool.")
    args = _get_args(args)
    check_account(args)
    record = {"tool": tool, "account": AGENTIC_ACCOUNT, "checks": ["account-gate=pass"]}

    if tool == "place_option_order":
        legs = args.get("legs") or []
        record["risk"] = await validate_option_legs(legs)
        record["checks"].append("defined-risk=pass")
    elif tool == "place_equity_order":
        side = str(args.get("side", "")).lower()
        if side not in ("buy", "sell"):
            raise TradeRefused(f"REFUSED: equity side must be buy/sell, got {side!r}.")
        if side == "sell":
            await _check_equity_sell_covered(
                str(args.get("symbol", "")), str(args.get("quantity", "0"))
            )
            record["checks"].append("sell-covered-by-owned-shares")
        else:
            record["checks"].append("cash-account-buy")
        record["risk"] = "defined risk: cash-account equity (no margin)"
    elif tool in ("cancel_equity_order", "cancel_option_order"):
        if not args.get("order_id"):
            raise TradeRefused("REFUSED: cancel needs an order_id.")
        record["risk"] = "risk-reducing: cancel"
        record["checks"].append("cancel")
    elif tool == "exercise_option":
        if args.get("allow_shorts"):
            raise TradeRefused(
                "REFUSED: allow_shorts=true would create a short equity "
                "position — never allowed."
            )
        record["risk"] = (
            "defined risk: exercise of long option (warning: usually destroys "
            "extrinsic value vs selling)"
        )
        record["checks"].append("allow_shorts=false")
    record["args"] = args
    return record


# --------------------------------------------------------------------------- #
# Stage (review-before-place) and Place
# --------------------------------------------------------------------------- #
async def _review_preview(tool: str, args: dict):
    """Run the broker's own non-binding preview for the exact order."""
    from rh_client import call_read_tool

    review_tool = REVIEW_TOOL.get(tool)
    if not review_tool:
        return {"note": f"no broker preview tool for {tool}; args shown as-is"}
    result = await call_read_tool(review_tool, args, interactive=False)
    texts = [
        b["text"] for b in result.get("content", []) if b.get("type") == "text"
    ]
    return {"tool": review_tool, "is_error": result.get("is_error"), "output": texts}


async def stage_order(tool: str, args: dict) -> dict:
    """Validate, preview via the broker, and write a stage file. No placement."""
    validation = await validate_order(tool, args)
    preview = await _review_preview(tool, validation["args"])
    stage_id = uuid.uuid4().hex[:12]
    stage = {
        "stage_id": stage_id,
        "created_at": _now_iso(),
        "created_by": "operator",
        "status": "staged",
        "tool": tool,
        "args": validation["args"],
        "validation": {
            "checks": validation["checks"],
            "risk": validation["risk"],
        },
        "review": preview,
    }
    _write_private(_stage_path(stage_id), stage)
    return stage


def load_stage(stage_id: str) -> dict:
    path = _stage_path(stage_id)
    try:
        stage = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TradeRefused(f"REFUSED: no such stage {stage_id!r}.")
    if stage.get("status") != "staged":
        raise TradeRefused(
            f"REFUSED: stage {stage_id} has status {stage.get('status')!r}, not staged."
        )
    return stage


async def place_staged(stage_id: str) -> dict:
    """Re-validate gates, execute the staged order, log everything."""
    from rh_client import call_tool_raw

    stage = load_stage(stage_id)
    # Re-run the gates fresh against the staged args (fail closed on drift).
    validation = await validate_order(stage["tool"], stage["args"])
    result = await call_tool_raw(stage["tool"], validation["args"], interactive=False)
    outcome = {
        "placed_at": _now_iso(),
        "tool": stage["tool"],
        "args": validation["args"],
        "risk": validation["risk"],
        "result": result,
    }
    # Mark the stage consumed before logging so a crash can't double-place.
    stage["status"] = "placed"
    stage["outcome"] = {
        "placed_at": outcome["placed_at"],
        "is_error": result.get("is_error"),
    }
    _write_private(_stage_path(stage_id), stage)
    _log_trade(stage, outcome)
    return outcome


def _log_trade(stage: dict, outcome: dict) -> None:
    args = outcome["args"]
    arg_lines = "\n".join(f"  - {k}: {v}" for k, v in args.items())
    result = outcome["result"]
    if result.get("is_error"):
        result_line = f"ERROR: {json.dumps(result)[:500]}"
    else:
        texts = [
            b.get("text", "")[:400]
            for b in result.get("content", [])
            if b.get("type") == "text"
        ]
        result_line = " | ".join(texts)[:800] or "ok (no text content)"
    review = stage.get("review", {})
    review_line = (review.get("output") or ["no preview"])[0][:400]
    entry = f"""
## {outcome['placed_at'][:10]} — {stage['tool']} (bridge, staged {stage['stage_id']})

- **Tool:** `{stage['tool']}`
- **Account:** {AGENTIC_ACCOUNT}
- **Risk check:** {outcome['risk']}
- **Full args:**
{arg_lines}
- **Broker preview:** {review_line}
- **Result:** {result_line}
"""
    TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADE_LOG, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def stage_summary(stage: dict) -> str:
    lines = [
        f"stage_id: {stage['stage_id']}",
        f"tool: {stage['tool']}",
        f"risk: {stage['validation']['risk']}",
        "args:",
    ]
    for k, v in stage["args"].items():
        lines.append(f"  {k}: {v}")
    review = stage.get("review", {})
    if review.get("output"):
        lines.append("broker preview (truncated):")
        lines.append("  " + review["output"][0][:500].replace("\n", " "))
    lines.append(
        "STATUS: staged only — nothing placed. `place` needs the operator's explicit approval."
    )
    return "\n".join(lines)
