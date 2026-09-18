#!/usr/bin/env python3
"""rh_mcp.py — read-only CLI for Robinhood's agentic trading MCP.

Commands:
  discover          No auth needed. Show the OAuth discovery chain
                    (protected-resource metadata -> authorization server).
  probe             No auth needed. POST initialize without credentials and
                    show the raw 401 + WWW-Authenticate response.
  auth-url          Run discovery + dynamic client registration, print the
                    authorization URL, then exit (browser step done separately).
  login [--then CMD] Full interactive flow: prints the authorization URL,
                    waits on the loopback redirect, exchanges the code, then
                    optionally runs --then (tools | discover).
  tools             Initialize + tools/list. Prints the inventory classified
                    as read/trade/mutate. (Requires login first, or
                    run `login --then tools`.)
  call NAME [--args '{...}']
                    Call one READ-ONLY tool. Trade/mutate tools are refused.

Tokens are held in process memory only and never written to disk.
Use the project's .venv python:  ./.venv/bin/python rh_mcp.py <command>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

# The SDK logs a traceback via logger.exception when the redirect handler
# raises; in headless mode that raise is the *expected* path (it carries the
# "run `login`" instructions), so keep the log from burying the message.
logging.getLogger("mcp.client.auth.oauth2").setLevel(logging.CRITICAL)

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from rh_auth import discover, probe_unauthenticated  # noqa: E402
from rh_client import call_read_tool, check_call_allowed, list_tools, print_inventory  # noqa: E402


def cmd_discover(_args):
    info = asyncio.run(discover())
    print(json.dumps(info, indent=2))


def cmd_probe(_args):
    info = asyncio.run(probe_unauthenticated())
    print(json.dumps(info, indent=2))


def cmd_auth_url(args):
    """Run discovery + dynamic client registration, print the authorization
    URL, then exit. The browser step is completed separately (see README)."""
    import secrets
    from urllib.parse import urlencode

    from mcp.client.auth.oauth2 import (
        create_client_registration_request,
        handle_registration_response,
    )
    from mcp.client.auth import PKCEParameters
    from mcp.shared.auth import OAuthClientMetadata
    from rh_auth import CALLBACK_PATH, MCP_URL, discover_metadata, make_http_client

    async def _run():
        asm, _auth_server_url, _metadata_url = await discover_metadata()
        if not asm.registration_endpoint:
            raise RuntimeError("Authorization server offers no registration endpoint")
        redirect_uri = f"http://127.0.0.1:{args.port}{CALLBACK_PATH}"
        client_metadata = OAuthClientMetadata(
            client_name="robinhood-mcp-client",
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="client_secret_post",
        )
        async with make_http_client(timeout=30) as http:
            req = create_client_registration_request(
                asm, client_metadata, str(asm.registration_endpoint)
            )
            resp = await http.send(req)
            client_info = await handle_registration_response(resp)

        pkce = PKCEParameters.generate()
        state = secrets.token_urlsafe(32)
        params = {
            "response_type": "code",
            "client_id": client_info.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
            "resource": MCP_URL,  # RFC 8707
        }
        if client_metadata.scope:
            params["scope"] = client_metadata.scope
        url = f"{asm.authorization_endpoint}?{urlencode(params)}"
        return url, client_info.client_id

    url, client_id = asyncio.run(_run())
    print("\n=== Open this URL in a browser to authorize ===\n")
    print(url)
    print(
        "\nRegistered OAuth client_id:", client_id,
        "\n\nLog into Robinhood on robinhood.com and approve the request, then run\n"
        "`rh_mcp.py login` (which listens for the redirect on this machine) to\n"
        "finish. Do NOT paste passwords anywhere.\n"
        "NOTE: the PKCE verifier/state above are single-use samples — `login`\n"
        "generates its own. Use `login` for the real flow.\n",
    )


def cmd_login(args):
    async def _run():
        if args.then == "tools":
            inventory = await list_tools(args.port, interactive=True)
            print_inventory(inventory)
            with open("/tmp/rh_tools.json", "w") as f:
                json.dump(inventory, f, indent=2)
            print("Inventory also saved to /tmp/rh_tools.json (ephemeral).")
        elif args.then == "discover":
            print(json.dumps(await discover(), indent=2))
        else:
            # Just complete auth and prove the session works.
            from rh_client import mcp_session

            async with mcp_session(args.port, interactive=True) as session:
                result = await session.list_tools()
            print(f"Authorized. Session live. {len(result.tools)} tools available.")
            print("Run `tools` to see the inventory.")

    _run_coro(_run())


def cmd_login_pasteback(args):
    """Paste-back login: for browsers that can't reach the loopback listener.

    Prints the authorization URL, then waits on stdin for the user to paste
    the full redirect URL from their browser's address bar after approving.
    """
    from rh_auth import PasteBackAuthFlow

    async def _run():
        flow = PasteBackAuthFlow()
        if args.then == "tools":
            inventory = await list_tools(args.port, interactive=True, auth_flow=flow)
            print_inventory(inventory)
            with open("/tmp/rh_tools.json", "w") as f:
                json.dump(inventory, f, indent=2)
            print("Inventory also saved to /tmp/rh_tools.json (ephemeral).")
        elif args.then == "discover":
            print(json.dumps(await discover(), indent=2))
        else:
            from rh_client import mcp_session

            async with mcp_session(
                args.port, interactive=True, auth_flow=flow
            ) as session:
                result = await session.list_tools()
            print(f"Authorized. Session live. {len(result.tools)} tools available.")

    _run_coro(_run())


def _auth_instructions(exc: BaseException) -> str | None:
    """Dig the headless-auth instructions out of wrapped exceptions."""
    seen = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, RuntimeError) and "Robinhood requires" in str(e):
            return str(e)
        for attr in ("exceptions", "__cause__", "__context__"):
            v = getattr(e, attr, None)
            if v is None:
                continue
            stack.extend(v if isinstance(v, (list, tuple)) else [v])
    return None


def _run_coro(coro):
    """Run a coroutine; surface auth-flow instructions cleanly."""
    try:
        return asyncio.run(coro)
    except BaseException as e:  # noqa: BLE001 - unwrap for a clean message
        instructions = _auth_instructions(e)
        if instructions:
            print(instructions, file=sys.stderr)
            sys.exit(2)
        # Full traceback: ExceptionGroups from the HTTP task group hide the
        # real cause in str(e); dump sub-exceptions too.
        import traceback

        traceback.print_exception(e)
        sys.exit(1)


def cmd_tools(args):
    inventory = _run_coro(list_tools(args.port, interactive=False))
    print_inventory(inventory)


def cmd_stage(args):
    """Validate + broker-preview an order. Writes a stage file; places nothing."""
    from rh_trade import stage_order, stage_summary

    try:
        tool_args = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as e:
        print(f"Bad --args JSON: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        stage = _run_coro(stage_order(args.tool, tool_args))
    except Exception as e:  # noqa: BLE001 - surface gate refusals cleanly
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    print(stage_summary(stage))


def cmd_place(args):
    """Execute a staged order. Requires --yes AND the operator's explicit approval."""
    from rh_trade import place_staged

    if not args.yes:
        print(
            "REFUSED: place needs --yes and the operator's explicit per-trade approval "
            "of this exact stage in chat. Nothing was placed.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        outcome = _run_coro(place_staged(args.stage))
    except Exception as e:  # noqa: BLE001
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(outcome, indent=2)[:2000])


def cmd_logout(args):
    """One-tap disconnect: delete the stored OAuth credentials."""
    from rh_auth import FileTokenStorage

    storage = FileTokenStorage()
    if storage.clear():
        print(f"Logged out. Deleted {storage.path}")
    else:
        print("No stored credentials found — already logged out.")


def cmd_status(args):
    """Show whether the bridge is seeded (without printing any secrets)."""
    import os
    import time

    from rh_auth import FileTokenStorage

    storage = FileTokenStorage()
    data = storage._read()
    has_tokens = bool(data.get("tokens"))
    has_client = bool(data.get("client_info"))
    obtained = data.get("obtained_at")
    age = f"{time.time() - obtained:.0f}s ago" if obtained else "unknown"
    try:
        mode = oct(os.stat(storage.path).st_mode & 0o777)
    except FileNotFoundError:
        mode = "no file"
    print(f"token file : {storage.path} ({mode})")
    print(f"tokens     : {'present' if has_tokens else 'absent'} (obtained {age})")
    print(f"client reg : {'present' if has_client else 'absent'}")
    if not has_tokens:
        print("Next step: run `login-pasteback` once to seed credentials.")


def cmd_call(args):
    try:
        check_call_allowed(args.name)
    except PermissionError as e:
        print(str(e), file=sys.stderr)
        sys.exit(3)
    try:
        tool_args = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as e:
        print(f"Bad --args JSON: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        result = _run_coro(
            call_read_tool(args.name, tool_args, args.port, interactive=False)
        )
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"Error: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))


def main():
    p = argparse.ArgumentParser(description="Read-only Robinhood agentic MCP client")
    p.add_argument("--port", type=int, default=8765, help="loopback OAuth callback port")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="unauthenticated OAuth discovery")
    sub.add_parser("probe", help="unauthenticated 401 probe")
    sub.add_parser("auth-url", help="print the browser authorization URL and exit")

    pl = sub.add_parser("login", help="interactive browser login, then optional action")
    pl.add_argument("--then", choices=["tools", "discover"], default=None)

    plb = sub.add_parser(
        "login-pasteback",
        help="login where you paste the redirect URL (browser can't reach loopback)",
    )
    plb.add_argument("--then", choices=["tools", "discover"], default=None)

    sub.add_parser("tools", help="list tools (needs prior login in same process)")

    pc = sub.add_parser("call", help="call one read-only tool")
    pc.add_argument("name")
    pc.add_argument("--args", default=None, help="JSON object of tool arguments")

    sub.add_parser("logout", help="delete stored OAuth credentials (disconnect)")
    sub.add_parser("status", help="show whether the bridge has stored credentials")

    ps = sub.add_parser("stage", help="validate + preview an order (places nothing)")
    ps.add_argument("--tool", required=True, help="write tool name, e.g. place_option_order")
    ps.add_argument("--args", default=None, help="JSON object of tool arguments")

    pp = sub.add_parser("place", help="execute a staged order (needs --yes + approval)")
    pp.add_argument("--stage", required=True, help="stage id from the stage command")
    pp.add_argument("--yes", action="store_true", help="explicit confirmation flag")

    args = p.parse_args()
    {
        "discover": cmd_discover,
        "probe": cmd_probe,
        "auth-url": cmd_auth_url,
        "login": cmd_login,
        "login-pasteback": cmd_login_pasteback,
        "tools": cmd_tools,
        "call": cmd_call,
        "logout": cmd_logout,
        "status": cmd_status,
        "stage": cmd_stage,
        "place": cmd_place,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
