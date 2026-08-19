"""HomeSense account lookups exposed as an MCP server over streamable HTTP.

Same `backend.py` functions the agent calls locally, published as MCP tools so
the agent can consume them over a network boundary instead of in-process.
That's the point of the demo beat: the tool contract the LLM sees is identical,
only the transport changes.

    uv run mcp_server.py                 # http://localhost:8770/mcp

Then in another terminal:

    USE_MCP=1 uv run agent.py dev

Every request must carry `Authorization: Bearer $MCP_TOKEN`, and every
account-scoped tool must carry `X-HomeSense-Account` matching the account it
asks about — the agent pins that header from its own verified session state, so
scope is enforced on this side of the trust boundary rather than trusting the
model to pass the right account number.
"""

from __future__ import annotations

import contextvars
import logging
import os
from typing import Any

import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

import backend

load_dotenv(".env.local")

logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(name)s  %(message)s")
logger = logging.getLogger("homesense.mcp")

HOST = os.getenv("MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("MCP_PORT", "8770"))
# Dev default: fine for a laptop demo, obviously not for anything real.
MCP_TOKEN = os.getenv("MCP_TOKEN", "homesense-dev-token")

mcp = FastMCP("homesense-backend", host=HOST, port=PORT)

# The verified account for the current HTTP request, set by the auth middleware
# from the X-HomeSense-Account header. A ContextVar rather than digging into MCP
# internals: it is set once per request and read by the tools below.
_scope_account: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "scope_account", default=None
)

# Tools the agent is allowed to call. lookup_by_account and create_ticket are
# published but NOT in the agent's allowed_tools list (see ALLOWED_MCP_TOOLS in
# agent.py): the first would let the model pull up any customer, the second
# would be a write path that skips CRM logging. They remain here so the server
# is a faithful stand-in for a real backend, and both are scope-enforced.
AGENT_EXPOSED_TOOLS = frozenset(
    {"get_recent_order", "get_device_status", "get_temperature", "get_subscription", "search_kb"}
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request without the shared bearer token.

    Deliberately the crudest thing that is still a real check, so the demo has
    something honest to show. Production would verify a JWT (or mTLS) and derive
    the caller's scope from its claims rather than trusting a header.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path == "/healthz":
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")

        if scheme.lower() != "bearer" or token != MCP_TOKEN:
            logger.warning(
                "auth REJECTED  path=%s  reason=%s",
                request.url.path,
                "missing token" if not token else "bad token",
            )
            return JSONResponse(
                {"error": "unauthorized", "detail": "Bearer token required"},
                status_code=401,
            )

        # The verified account travels as a header, set by the agent when it
        # builds the MCP connection. Tools enforce their arguments against it,
        # so scope is checked on this side of the trust boundary.
        scope = request.headers.get("x-homesense-account")
        _scope_account.set(scope)
        logger.info(
            "auth ok · scope=account:%s · path=%s", scope or "unscoped", request.url.path
        )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Tools - thin wrappers over the same backend the local tools use
# ---------------------------------------------------------------------------


def _require(account_number: str) -> backend.Customer:
    """Resolve an account, enforcing it against the request's verified scope.

    The model supplies the account number as a tool argument, so it cannot be
    trusted on its own. The scope header is set by the agent from its own
    session state, and a mismatch is refused here rather than relying on the
    agent-side guard.
    """
    scope = _scope_account.get()
    if scope is None:
        logger.warning("scope REFUSED · no X-HomeSense-Account header")
        raise ValueError(
            "This request carries no verified account scope. Identify the caller first."
        )
    if backend.normalize_account(scope) != backend.normalize_account(account_number):
        logger.warning(
            "scope REFUSED · requested=%s verified=%s", account_number, scope
        )
        raise ValueError(
            "I can only help with the verified account. Refuse and do not reveal "
            "anything about another account."
        )

    customer = backend.lookup_by_account(account_number)
    if customer is None:
        raise ValueError(f"No HomeSense account matches '{account_number}'.")
    return customer


# NOT exposed to the agent (see AGENT_EXPOSED_TOOLS above): it accepts an
# arbitrary account number, so exposing it would let the model pull up any
# customer. Kept here because a real backend has this endpoint, and _require
# refuses any account that disagrees with the request's scope header.
@mcp.tool()
def lookup_by_account(account_number: str) -> dict[str, Any]:
    """Look up a HomeSense customer by account number.

    Args:
        account_number: Account number as the caller said it, e.g. "HS-10042".
    """
    customer = _require(account_number)
    logger.info("TOOL lookup_by_account(%s) -> %s", account_number, customer.name)
    return {
        "first_name": customer.first_name,
        "full_name": customer.name,
        "account_number": customer.account_number,
        "tier": customer.tier,
    }


@mcp.tool()
def get_recent_order(account_number: str) -> dict[str, Any]:
    """Get the status of a customer's most recent order.

    Args:
        account_number: The verified account number.
    """
    customer = _require(account_number)
    order = backend.get_recent_order(customer)
    if order is None:
        raise ValueError(f"Account {customer.account_number} has no orders on file.")
    logger.info("TOOL get_recent_order(%s) -> %s", account_number, order["order_id"])
    return {**order, "spoken_summary": backend.spoken_order_status(order)}


@mcp.tool()
def get_device_status(account_number: str, device_name: str) -> dict[str, Any]:
    """Check whether one of a customer's devices is active, online or offline.

    Args:
        account_number: The verified account number.
        device_name: Device as the caller described it, e.g. "basement sensor".
    """
    customer = _require(account_number)
    try:
        device = backend.get_device_status(customer, device_name)
    except backend.BackendError as exc:
        raise ValueError(str(exc)) from exc
    logger.info(
        "TOOL get_device_status(%s, %r) -> %s", account_number, device_name, device["status"]
    )
    return device


@mcp.tool()
def get_temperature(account_number: str, room: str) -> dict[str, Any]:
    """Read the current temperature from the thermostat in a room.

    Args:
        account_number: The verified account number.
        room: The room the caller named, e.g. "living room".
    """
    customer = _require(account_number)
    try:
        reading = backend.get_temperature(customer, room)
    except backend.BackendError as exc:
        raise ValueError(str(exc)) from exc
    logger.info("TOOL get_temperature(%s, %r) -> %s", account_number, room, reading["temperature_f"])
    return reading


@mcp.tool()
def get_subscription(account_number: str) -> dict[str, Any]:
    """Get a customer's cloud video subscription plan, status and renewal date.

    Args:
        account_number: The verified account number.
    """
    customer = _require(account_number)
    subscription = backend.get_subscription_status(customer)
    logger.info("TOOL get_subscription(%s) -> %s", account_number, subscription.get("status"))
    return subscription


@mcp.tool()
def search_kb(query: str) -> dict[str, Any]:
    """Search the HomeSense knowledge base for how-to and policy answers.

    Args:
        query: The caller's question, e.g. "how do I reset my thermostat".
    """
    articles = backend.search_kb(query)
    logger.info("TOOL search_kb(%r) -> %s", query, [a["title"] for a in articles])
    return {"articles": articles, "count": len(articles)}


# NOT exposed to the agent (see AGENT_EXPOSED_TOOLS above). Ticketing runs on
# the local function tool so it reaches the CRM log, state.tickets, the human
# console and the session report. Published here only so the server is a
# faithful stand-in for a real backend; still scope-enforced via _require.
@mcp.tool()
def create_ticket(
    account_number: str, issue_summary: str, device_id: str | None = None, priority: str = "normal"
) -> dict[str, Any]:
    """File a support ticket for an unresolved issue.

    Args:
        account_number: The verified account number.
        issue_summary: What is wrong and what was already tried.
        device_id: Affected device ID, if known.
        priority: "low", "normal" or "high".
    """
    customer = _require(account_number)
    ticket = backend.create_ticket(
        account_number=customer.account_number,
        customer_name=customer.name,
        issue_summary=issue_summary,
        device_id=device_id,
        priority=priority,
    )
    logger.info("TOOL create_ticket(%s) -> %s", account_number, ticket["ticket_id"])
    return ticket


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def build_app():  # type: ignore[no-untyped-def]
    """The Starlette app with auth wrapped around it (also used by tests)."""
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    return app


def main() -> None:
    url = f"http://{HOST}:{PORT}/mcp"
    logger.info("HomeSense MCP server on %s", url)
    logger.info(
        "tools: %s", ", ".join(sorted(AGENT_EXPOSED_TOOLS)) + " (exposed to the agent)"
    )
    logger.info(
        "tools: lookup_by_account, create_ticket (published, NOT in the agent's allowed_tools)"
    )
    logger.info("auth: Authorization: Bearer <MCP_TOKEN>  (set MCP_TOKEN in .env.local)")
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
