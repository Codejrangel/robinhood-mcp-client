# Robinhood MCP Client

A safety-first Python client for [Robinhood's agentic trading MCP](https://agent.robinhood.com/mcp/trading)
(MCP over Streamable HTTP). Read market data, preview orders, and — only if you
explicitly enable it — place trades through a staged, gated write path where
every order is validated, broker-previewed, and logged before it can execute.

The core idea: **an agent (or a human) should never be one function call away
from moving real money.** Reads are free; writes must pass five gates.

## Safety model

| # | Gate | What it does |
|---|------|--------------|
| 1 | Account gate | Writes are refused unless the order targets the single designated account (`RH_ACCOUNT_NUMBER`). Your main portfolio can never be touched by accident. |
| 2 | Defined-risk | Long options always pass (max loss = premium). Any sell-to-open leg must sit inside a same-expiry, same-underlying spread with every short leg covered further OTM. Naked shorts, calendars, and diagonals are refused. Equity sells are checked against owned shares — shorting is impossible. |
| 3 | Review-before-place | `place` only executes a stage created by `stage`, which embeds the broker's own `review_*` preview of the exact order. No preview, no placement. |
| 4 | Stage TTL | Stages expire after 24h. A stale preview can't be executed — re-stage for a fresh one. |
| 5 | Visible log | Every placement appends timestamp, full args, risk check, broker preview, and result to `trade-log.md`. |

On top of the code gates: `place` requires `--yes` **and** the operator's
explicit per-trade approval of that exact stage. Automated jobs should stage
proposals, never place.

## Layout

| File | Purpose |
|---|---|
| `rh_mcp.py` | CLI entry point |
| `rh_auth.py` | OAuth: discovery, dynamic client registration (RFC 7591), loopback/paste-back callback flows, file-backed token storage (0600) with silent refresh |
| `rh_client.py` | MCP session, `tools/list`, read-only policy gate (unknown tools fail closed) |
| `rh_trade.py` | Staged write path: account gate, defined-risk validation, broker preview, place, trade log |
| `TOOLS.md` | Observed tool inventory of the upstream server |

## Requirements

- Python 3.12+
- A Robinhood account with an **Agentic** sub-account (the MCP only works against agentic-enabled accounts)

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Quickstart

**1. Point the client at your account** (writes are refused until this is set):

```bash
export RH_ACCOUNT_NUMBER="your-agentic-account-number"
```

**2. Authenticate** (one interactive step — you log in on robinhood.com itself,
no password is ever pasted or stored here):

```bash
./.venv/bin/python rh_mcp.py login --then tools
```

This prints an authorization URL. Open it, approve, and the loopback listener
captures the redirect. If your browser can't reach the loopback address, use
`login-pasteback` instead and paste the redirect URL back.

Tokens persist in `~/.config/robinhood-mcp/tokens.json` (0600) and refresh
silently — later runs need no browser. `logout` deletes the file (one-tap
disconnect); `status` shows whether credentials are seeded without printing
secrets.

**3. Read:**

```bash
./.venv/bin/python rh_mcp.py call get_equity_quotes --args '{"symbols": "AAPL"}'
./.venv/bin/python rh_mcp.py call get_option_chains --args '{"symbol": "AAPL"}'
```

Trade/mutate tools are refused by `call` — reads only.

**4. Stage and place (writes):**

```bash
# Validate + broker-preview. Places nothing, prints a stage id.
./.venv/bin/python rh_mcp.py stage --tool place_option_order --args '{...}'

# Execute a staged order. Needs --yes AND your explicit approval of that stage.
./.venv/bin/python rh_mcp.py place --stage <stage_id> --yes
```

Every placement is appended to `trade-log.md` (gitignored — your history stays yours).

## How the OAuth works

Standard MCP-spec flow, no reverse engineering:

1. Unauthenticated `initialize` → `401` with `WWW-Authenticate` pointing at the
   protected-resource metadata.
2. Client fetches authorization-server metadata (issuer, authorization/token/
   registration endpoints).
3. Client dynamically registers itself (RFC 7591) and builds the authorization
   URL (PKCE S256, `state`, RFC 8707 `resource`).
4. You approve on robinhood.com; the code is exchanged for access + refresh tokens.

`discover` and `probe` run these steps without credentials if you want to
inspect the handshake first.

## Disclaimer

Educational project, not financial advice. Options involve risk, including total
loss of premium. The upstream MCP is unofficial and may change or break without
notice. Test with small amounts, keep `review-before-place` on, and never
connect an account you can't afford to have traded.

## License

MIT — see [LICENSE](LICENSE).
