"""OAuth for Robinhood's agentic trading MCP.

Flow (MCP spec / RFC 8707 + RFC 7591 + RFC 8414):
  1. Client hits the MCP endpoint unauthenticated -> 401 with
     WWW-Authenticate pointing at the protected-resource metadata.
  2. Client discovers the authorization server metadata.
  3. Client dynamically registers itself (DCR) and gets client_id/secret.
  4. User opens the authorization URL in a browser, logs into Robinhood,
     approves the agentic-trading scopes.
  5. Robinhood redirects to our loopback listener with an auth code.
  6. We exchange the code for access + refresh tokens (tokens last ~7 days observed).

Security rules for this project:
  * OAuth tokens + the dynamically-registered client info persist in a
    single 0600 file under ~/.config/robinhood-mcp/ (this private VM only).
    They are never printed, logged, pasted into chat, or committed anywhere.
  * `logout` deletes that file: one-tap disconnect.
  * No passwords are asked for or stored; the user authenticates in their
    own browser on robinhood.com.
  * The interactive browser step is cleanly separated: `auth-url` prints the
    URL and waits; whoever drives the browser (parent agent / user) completes
    it, and the loopback server captures the redirect on this machine.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anyio

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    handle_auth_metadata_response,
    handle_protected_resource_response,
)
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

MCP_URL = "https://agent.robinhood.com/mcp/trading"
CALLBACK_PATH = "/oauth/callback"
SUCCESS_PAGE = b"<html><body><h2>Authorized</h2><p>You can close this tab.</p></body></html>"


def make_http_client(**kwargs):
    """httpx2 client routed through the sandbox egress proxy.

    The MCP SDK's auth machinery speaks httpx2 (its Request/Auth types), so
    we use httpx2 throughout. httpx2's env-var proxy parsing chokes on this
    sandbox's NO_PROXY list (IPv6 bracket entries), so we set the proxy
    explicitly and ignore env.
    """
    import os

    import httpx2

    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    kwargs.setdefault("proxy", proxy)
    kwargs.setdefault("trust_env", False)
    return httpx2.AsyncClient(**kwargs)


# --------------------------------------------------------------------------- #
# In-memory token storage: process lifetime only (kept for tests/ephemeral).
# --------------------------------------------------------------------------- #
class MemoryTokenStorage(TokenStorage):
    """Holds tokens + registered client info for the life of the process."""

    def __init__(self) -> None:
        self._tokens: OAuthToken | None = None
        self._obtained_at: float = 0.0
        self._client_info: OAuthClientInformationFull | None = None
        self._server_metadata: OAuthMetadata | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens
        self._obtained_at = time.time()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info

    # Helpers the CLI uses (not part of TokenStorage)
    def token_age(self) -> float:
        return time.time() - self._obtained_at if self._tokens else float("inf")

    def save_server_metadata(self, md: OAuthMetadata) -> None:
        self._server_metadata = md

    def load_server_metadata(self) -> OAuthMetadata | None:
        return self._server_metadata


# --------------------------------------------------------------------------- #
# File-backed token storage: the always-on bridge.
# --------------------------------------------------------------------------- #
TOKEN_DIR = Path.home() / ".config" / "robinhood-mcp"
TOKEN_FILE = TOKEN_DIR / "tokens.json"


class FileTokenStorage(TokenStorage):
    """Persists tokens + registered client info to a 0600 file.

    This is what makes the bridge durable: one interactive OAuth seeds the
    file, and every later run (interactive or scheduled) loads it and lets
    the SDK silently refresh the access token via the refresh_token grant.
    No browser, no user, no ChatGPT in the loop after seeding.

    The file holds bearer credentials: directory is 0700, file is 0600,
    written via temp-file + atomic rename. `clear()` deletes it (logout).
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else TOKEN_FILE
        self._data: dict = self._read()

    # -- persistence ------------------------------------------------------ #
    def _read(self) -> dict:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._path.parent, 0o700)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)

    def clear(self) -> bool:
        """Delete the stored credentials. Returns True if anything existed."""
        try:
            self._path.unlink()
            self._data = {}
            return True
        except FileNotFoundError:
            self._data = {}
            return False

    @property
    def path(self) -> Path:
        return self._path

    # -- TokenStorage ------------------------------------------------------ #
    async def get_tokens(self) -> OAuthToken | None:
        raw = self._data.get("tokens")
        if not raw:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception:  # corrupt entry: treat as absent, don't crash
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = tokens.model_dump(mode="json")
        self._data["obtained_at"] = time.time()
        self._write()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._data.get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._data["client_info"] = client_info.model_dump(mode="json")
        self._write()

    # Helpers the CLI uses (not part of TokenStorage)
    def token_age(self) -> float:
        obtained = self._data.get("obtained_at")
        if not self._data.get("tokens") or not obtained:
            return float("inf")
        return time.time() - obtained

    def stored_token_expiry(self) -> float | None:
        """Absolute expiry timestamp for the stored access token, if known.

        The MCP SDK only attempts the refresh_token grant when its own
        expiry clock says the token is expired — but it never sets that
        clock for tokens loaded from disk, so refresh was silently dead
        and the first 401 went straight to interactive auth (2026-09-24).
        Seed the provider's clock from obtained_at + expires_in so refresh
        fires as designed.
        """
        tokens = self._data.get("tokens") or {}
        obtained_at = self._data.get("obtained_at")
        expires_in = tokens.get("expires_in")
        if not obtained_at or expires_in is None:
            return None
        try:
            return float(obtained_at) + int(expires_in)
        except (TypeError, ValueError):
            return None

    def save_server_metadata(self, md: OAuthMetadata) -> None:
        self._data["server_metadata"] = md.model_dump(mode="json")
        self._write()

    def load_server_metadata(self) -> OAuthMetadata | None:
        raw = self._data.get("server_metadata")
        if not raw:
            return None
        try:
            return OAuthMetadata.model_validate(raw)
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# Loopback callback server: captures the ?code=... redirect on this machine.
# --------------------------------------------------------------------------- #
class _CallbackHandler(BaseHTTPRequestHandler):
    query: dict = {}

    def do_GET(self):  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        type(self).query = parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(SUCCESS_PAGE)

    def log_message(self, *args):  # noqa: ANN002,ANN202
        pass


def _serve_one_callback(port: int, timeout: float = 600.0) -> dict:
    """Block until the OAuth provider redirects back to loopback."""
    _CallbackHandler.query = {}
    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.timeout = 1.0
    deadline = time.time() + timeout
    try:
        while not _CallbackHandler.query and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not _CallbackHandler.query:
        raise TimeoutError("Timed out waiting for the OAuth redirect.")
    return _CallbackHandler.query


class LoopbackAuthFlow:
    """Redirect handler prints the URL; callback waits on the loopback server.

    The printed URL is handed to whoever drives the browser (parent agent's
    browser task on this machine, or the user). No credentials are pasted
    anywhere: the user logs in on robinhood.com itself.
    """

    def __init__(self, port: int, timeout: float = 600.0) -> None:
        self.port = port
        self.timeout = timeout

    async def redirect(self, authorization_url: str) -> None:
        print("\n=== ACTION REQUIRED: open this URL in a browser ===\n")
        print(authorization_url)
        print(
            "\nLog into Robinhood and approve the request. "
            "This tool is listening for the redirect — no code to paste.\n"
        )

    async def callback(self) -> AuthorizationCodeResult:
        query = await anyio.to_thread.run_sync(
            _serve_one_callback, self.port, self.timeout
        )
        if "error" in query:
            raise RuntimeError(f"Authorization failed: {query['error'][0]}")
        if "code" not in query:
            raise RuntimeError("Authorization callback did not include a code")
        return AuthorizationCodeResult(
            code=query["code"][0],
            state=query.get("state", [None])[0],
        )


class HeadlessAuthFlow:
    """Refuses interactive auth; used by non-interactive commands."""

    async def redirect(self, authorization_url: str) -> None:
        raise RuntimeError(
            "Robinhood requires interactive authorization.\n"
            "Run `rh_mcp.py login-pasteback`, open the printed URL in a browser,\n"
            "approve, then paste back the redirect URL:\n"
            f"{authorization_url}"
        )

    async def callback(self) -> AuthorizationCodeResult:  # pragma: no cover
        raise RuntimeError("unreachable: redirect() always raises first")


class PasteBackAuthFlow:
    """Redirect handler for browsers that can't reach the loopback listener.

    Prints the authorization URL. The user completes approval in their own
    browser, where the redirect to 127.0.0.1 fails to connect — expected and
    harmless. They copy the full address-bar URL (it carries ?code=...) and
    paste it at the prompt. Nothing is written to disk; the code is
    single-use and expires within minutes.
    """

    def __init__(self, timeout: float = 600.0) -> None:
        self.timeout = timeout

    async def redirect(self, authorization_url: str) -> None:
        print("\n=== ACTION REQUIRED: open this URL in a browser ===\n")
        print(authorization_url)
        print(
            "\nLog into Robinhood and approve the request. Your browser will "
            "then fail to connect to 127.0.0.1 — that's expected and harmless.\n"
            "Copy the FULL URL from the address bar (it contains ?code=...)\n"
            "and paste it at the prompt below.\n"
        )

    async def callback(self) -> AuthorizationCodeResult:
        print("Paste the redirect URL from your browser's address bar, then Enter:")
        try:
            with anyio.fail_after(self.timeout):
                line = await anyio.to_thread.run_sync(sys.stdin.readline)
        except TimeoutError:
            raise RuntimeError("Timed out waiting for the pasted redirect URL.")
        query = parse_qs(urlparse(line.strip()).query)
        if "error" in query:
            raise RuntimeError(f"Authorization failed: {query['error'][0]}")
        if "code" not in query:
            raise RuntimeError(
                "That URL did not contain an authorization code (?code=...)."
            )
        return AuthorizationCodeResult(
            code=query["code"][0],
            state=query.get("state", [None])[0],
        )


def build_provider(
    port: int = 8765,
    *,
    interactive: bool = True,
    flow=None,
    storage: TokenStorage | None = None,
) -> tuple[OAuthClientProvider, TokenStorage]:
    """Build the OAuth client provider + token storage.

    Pass `flow` to override the redirect/callback handler (e.g.
    PasteBackAuthFlow). Storage defaults to FileTokenStorage so tokens and
    the registered client survive across runs and refresh silently; pass
    MemoryTokenStorage() explicitly for a purely ephemeral session.
    """
    metadata = OAuthClientMetadata(
        client_name="robinhood-mcp-client",
        redirect_uris=[f"http://127.0.0.1:{port}{CALLBACK_PATH}"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
    )
    storage = storage or FileTokenStorage()
    flow = flow or (LoopbackAuthFlow(port) if interactive else HeadlessAuthFlow())
    provider = OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=flow.redirect,
        callback_handler=flow.callback,
    )
    # Seed the SDK's token-expiry clock from the stored token's real age.
    # Without this, is_token_valid() is True forever for disk-loaded tokens,
    # the refresh grant never fires, and the first 401 demands interactive
    # auth (root cause of the 2026-09-24 outage). After a successful refresh
    # the SDK maintains the clock itself via update_token_expiry().
    seed_expiry = getattr(storage, "stored_token_expiry", None)
    if callable(seed_expiry):
        expiry = seed_expiry()
        if expiry is not None:
            provider.context.token_expiry_time = expiry
    return provider, storage


# --------------------------------------------------------------------------- #
# Unauthenticated discovery: what the 401/WWW-Authenticate dance would find.
# --------------------------------------------------------------------------- #
async def discover_metadata() -> tuple[OAuthMetadata, str | None, str | None]:
    """Fetch authorization-server metadata object (no auth needed).

    Returns (metadata, auth_server_url, protected_resource_url).
    """
    async with make_http_client(follow_redirects=True, timeout=30) as http:
        prm = None
        for url in build_protected_resource_metadata_discovery_urls(None, MCP_URL):
            resp = await http.send(create_oauth_metadata_request(url))
            prm = await handle_protected_resource_response(resp)
            if prm is not None:
                break
        auth_server_url = (
            str(prm.authorization_servers[0]) if prm else None
        )
        for url in build_oauth_authorization_server_metadata_discovery_urls(
            auth_server_url, MCP_URL
        ):
            resp = await http.send(create_oauth_metadata_request(url))
            ok, asm = await handle_auth_metadata_response(resp)
            if not ok:
                raise RuntimeError(f"Authorization server rejected metadata at {url}")
            if asm is not None:
                return asm, auth_server_url, url
    raise RuntimeError("Could not discover authorization server metadata")


async def discover() -> dict:
    """Fetch protected-resource + authorization-server metadata (no auth)."""
    asm, auth_server_url, metadata_url = await discover_metadata()
    return {
        "mcp_url": MCP_URL,
        "protected_resource_metadata_url": (
            "https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading"
        ),
        "resource": "https://agent.robinhood.com/mcp/trading",
        "auth_server_url": auth_server_url,
        "auth_server_metadata_url": metadata_url,
        "issuer": str(asm.issuer),
        "authorization_endpoint": str(asm.authorization_endpoint),
        "token_endpoint": str(asm.token_endpoint),
        "registration_endpoint": (
            str(asm.registration_endpoint) if asm.registration_endpoint else None
        ),
        "scopes_supported": list(asm.scopes_supported or []),
        "code_challenge_methods": list(asm.code_challenge_methods_supported or []),
    }


# --------------------------------------------------------------------------- #
# Raw 401 probe: show exactly what the endpoint answers without credentials.
# --------------------------------------------------------------------------- #
async def probe_unauthenticated() -> dict:
    """POST an MCP initialize without auth; report status + WWW-Authenticate."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "rh-mcp-probe", "version": "0.1"},
        },
    }
    async with make_http_client(timeout=30) as http:
        resp = await http.post(
            MCP_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    return {
        "status": resp.status_code,
        "www_authenticate": resp.headers.get("www-authenticate"),
        "content_type": resp.headers.get("content-type"),
        "body_snippet": resp.text[:400],
    }
