"""HomeSense customer-service voice agent (LiveKit Agents 1.6.x).

Five-block anatomy:

    1. IMPORTS & SERVER SETUP  - the worker process that registers with LiveKit
    2. PREWARM                 - cold-start cost paid once per process, not per call
    3. AGENT CLASSES           - IdentifyAgent -> SupportAgent, and their tools
    4. ENTRYPOINT              - runs once per call, in its own OS process
    5. RUN                     - hand the server to the CLI

Run it:
    uv run agent.py console     # talk to it in the terminal
    uv run agent.py dev         # register with LiveKit, join from the web app
    USE_MCP=1 uv run agent.py dev    # read-only lookups over MCP instead
"""

from __future__ import annotations

# ─── 1. IMPORTS & SERVER SETUP ──────────────────────────────────────────────
import asyncio
import contextlib
import json
import logging
import os
import textwrap
import time
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any, Literal

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    PreemptiveGenerationOptions,
    RunContext,
    ToolError,
    TurnHandlingOptions,
    function_tool,
    inference,
)
from livekit.agents import (
    metrics as lk_metrics,
)
from livekit.agents.inference.tts import CartesiaOptions
from livekit.agents.llm import mcp as lk_mcp
from livekit.plugins import silero

import backend
from backend import BackendError, Customer

load_dotenv(".env.local")

logger = logging.getLogger("homesense")

# LiveKit Inference: models are plain "provider/model" strings resolved and
# billed through your LiveKit project, so only LIVEKIT_URL / API_KEY /
# API_SECRET are needed - no Deepgram, OpenAI or Cartesia account.
STT_MODEL = os.getenv("STT_MODEL", "deepgram/nova-3")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4.1-mini")
TTS_MODEL = os.getenv("TTS_MODEL", "cartesia/sonic-2")
# Cartesia accepts only the enum here - a float is rejected by LiveKit
# Inference with "invalid speed value", so do not "tune" this to a number.
TTS_SPEED = os.getenv("TTS_SPEED", "slow")

# Inbound SIP: rooms created by the dispatch rule are named "call-<something>".
SIP_ROOM_PREFIX = os.getenv("SIP_ROOM_PREFIX", "call-")

# Read-only lookups over MCP instead of in-process function tools.
USE_MCP = os.getenv("USE_MCP", "").strip() in ("1", "true", "yes")
MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8770/mcp")
MCP_TOKEN = os.getenv("MCP_TOKEN", "homesense-dev-token")

# Warm-transfer target. When both are set the agent dials a real human over SIP;
# otherwise it falls back to the simulated console handoff (see block 3).
TRANSFER_PHONE_NUMBER = os.getenv("TRANSFER_PHONE_NUMBER", "").strip()
SIP_OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID", "").strip()

# Turn-taking / session guards.
# How long to wait for the caller to appear before giving up on caller ID /
# web session metadata and just asking for the account number.
PARTICIPANT_WAIT_SECONDS = float(os.getenv("PARTICIPANT_WAIT_SECONDS", "5"))
# Greeting into a not-yet-subscribed track is the classic "agent talked before
# I could hear it" bug: wait for the caller's audio, then let it settle.
TRACK_WAIT_SECONDS = float(os.getenv("TRACK_WAIT_SECONDS", "3"))
GREETING_SETTLE_SECONDS = float(os.getenv("GREETING_SETTLE_SECONDS", "0.5"))
USER_AWAY_TIMEOUT = float(os.getenv("USER_AWAY_TIMEOUT", "15"))
MAX_SESSION_SECONDS = float(os.getenv("MAX_SESSION_SECONDS", "900"))  # 15 minutes

EscalationReason = Literal["requested", "irate", "unresolved", "policy"]

# Which backend each tool talks to, for the flow visualiser's edge animation.
TOOL_TARGETS: dict[str, str] = {
    "identify_by_account_number": "db",
    "identify_by_phone_number": "db",
    "get_recent_order_status": "db",
    "get_device_status": "db",
    "get_room_temperature": "db",
    "get_subscription_status": "db",
    "list_registered_devices": "db",
    "search_kb": "kb",
    "create_support_ticket": "ticketing",
    "add_account_note": "crm",
    "transfer_to_human": "human",
}


def tool_target(tool: str) -> str:
    """Subsystem a tool call lands on. Read-only lookups move to MCP when enabled."""
    target = TOOL_TARGETS.get(tool, "db")
    if USE_MCP and target in ("db", "kb"):
        return "mcp"
    return target

# Chatty third-party loggers drown the per-turn latency line.
_NOISY_LOGGERS = (
    "livekit",
    "livekit.agents",
    "livekit.agents.inference",
    "livekit.plugins",
    "aiohttp",
    "aiohttp.access",
    "websockets",
    "httpx",
    "httpcore",
    "urllib3",
    "mcp",
    "numba",
    "torch",
)


def configure_logging() -> None:
    """Keep the agent's own lines readable during a live demo."""
    logger.setLevel(logging.INFO)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def log_stack() -> None:
    """One line that shows the whole LiveKit stack at a glance."""
    logger.info(
        "STACK: STT=%s LLM=%s TTS=%s via LiveKit Inference · VAD=silero · "
        "turn=inference.TurnDetector · preemptive_generation=True · dispatch=automatic",
        STT_MODEL,
        LLM_MODEL,
        f"{TTS_MODEL} (speed={TTS_SPEED})" if TTS_MODEL.startswith("cartesia/") else TTS_MODEL,
    )
    if USE_MCP:
        logger.info("TOOLS: via MCP (%s)", MCP_URL)
    else:
        logger.info("TOOLS: local function tools")


def build_tts() -> Any:
    """TTS for the session, slowed down for a calmer phone manner.

    Speed is a Cartesia-specific option, passed through LiveKit Inference as
    extra_kwargs - no Cartesia account or API key involved. Only the string
    enum ("slow" / "normal" / "fast") is accepted; floats are rejected by the
    gateway, so a non-cartesia model just gets the plain model string.
    """
    if TTS_MODEL.startswith("cartesia/") and TTS_SPEED:
        return inference.TTS(
            model=TTS_MODEL, extra_kwargs=CartesiaOptions(speed=TTS_SPEED)
        )
    return TTS_MODEL


server = AgentServer()


# ─── 2. PREWARM (once per process, BEFORE any job) ──────────────────────────
def prewarm(proc: JobProcess) -> None:
    """Load the Silero VAD weights at process start.

    The worker keeps idle processes warm, so this model load is paid at pod
    startup rather than being added to the first caller's latency.
    """
    # Runs after the CLI has configured logging, so this is the earliest point
    # in the job process where quietening the noisy loggers actually sticks.
    configure_logging()
    proc.userdata["vad"] = silero.VAD.load()
    logger.info("prewarm complete: silero VAD loaded")


server.setup_fnc = prewarm


# ─── 3. AGENT CLASSES ───────────────────────────────────────────────────────
@dataclass
class CallState:
    """Everything the agent learns during one call.

    Held in AgentSession.userdata so every tool can read and update it via
    RunContext.userdata, and so the shutdown callback can write a report.
    """

    session_id: str = "unknown"
    room_name: str = "unknown"
    channel: str = "web"  # "phone" once a SIP caller is identified
    phase: str = "identify"  # -> "support"
    caller_phone: str | None = None
    identified_via: str | None = None  # caller_id | web_session | account_number
    customer: Customer | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tickets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    kb_articles: list[str] = field(default_factory=list)
    handoff: dict[str, Any] | None = None
    escalation_reason: str | None = None
    away_prompts: int = 0
    rehydrated: bool = False
    # True once a human has been dialled in over SIP: the room must outlive
    # the agent so the caller and the specialist can keep talking.
    sip_transfer_active: bool = False

    def emit(self, kind: str, **fields: Any) -> None:
        """Publish one event to the live flow/stats stream. Never raises."""
        backend.emit_event(
            kind,
            session_id=self.session_id,
            room=self.room_name,
            account_number=self.customer.account_number if self.customer else None,
            **fields,
        )

    @property
    def identified(self) -> bool:
        return self.customer is not None

    def require_customer(self) -> Customer:
        """Guard every account tool: no identity, no data."""
        if self.customer is None:
            raise ToolError(
                "No verified caller on this session yet, so account data is not "
                "available. Do not answer anything about an account."
            )
        return self.customer

    @staticmethod
    def _refers_to_verified(supplied: str | None, customer: Customer) -> bool:
        """Does a model-supplied identifier refer to the already-verified caller?

        The model fills the optional `account_number` argument with whatever the
        caller last said to identify themselves - which is often NOT the account
        number. After identify_by_phone_number it passes the phone digits; after
        a spoken account number it passes the bare digits ("10042"). A strict
        string compare against "HS-10042" refuses all of those, which locked a
        correctly-identified caller out of every tool.

        So accept anything that resolves to the verified caller, and refuse only
        an identifier that clearly points somewhere else.
        """
        if not supplied or not supplied.strip():
            return True

        if backend.normalize_account(supplied) == backend.normalize_account(
            customer.account_number
        ):
            return True

        digits = backend.account_digits(supplied)
        if not digits:
            # No digits at all - nothing that could name another account.
            return True

        account = backend.account_digits(customer.account_number)
        if account and account in digits:
            return True

        phone = backend.normalize_phone(customer.phone)
        if phone and backend.normalize_phone(supplied) == phone:
            return True

        return False

    def require_scope(self, account_number: str | None) -> Customer:
        """Identity gate plus scope check.

        Refuses only when the model names a genuinely different account - see
        _refers_to_verified for why a strict compare is wrong here.
        """
        customer = self.require_customer()
        if not self._refers_to_verified(account_number, customer):
            logger.warning(
                "SCOPE REFUSED requested=%s verified=%s",
                account_number,
                customer.account_number,
            )
            raise ToolError(
                f"That is a different account. You are helping {customer.preferred_name} "
                f"with account {customer.account_number} on this call. Tell the caller you "
                "can only discuss their own account, then carry on helping them with it. "
                "Do NOT ask them to verify again."
            )
        return customer

    def note_tool(self, tool: str, detail: dict[str, Any]) -> None:
        """Record a tool call locally, log it, and mirror it into the CRM."""
        entry = {"tool": tool, **detail}
        self.tool_calls.append(entry)

        # One readable line per tool call, e.g.
        #   TOOL create_support_ticket -> TKT-1001
        outcome = (
            detail.get("ticket_id")
            or detail.get("handoff_id")
            or detail.get("titles")
            or detail.get("account_number")
            or detail.get("order_id")
            or detail.get("device_id")
            or detail.get("result")
            or "ok"
        )
        logger.info("TOOL %s -> %s", tool, outcome)
        self.emit(
            "tool_call",
            tool=tool,
            target=tool_target(tool),
            summary=", ".join(outcome) if isinstance(outcome, list) else str(outcome),
            result=detail.get("result"),
        )

        backend.crm_log(
            {
                "session_id": self.session_id,
                "room": self.room_name,
                "channel": self.channel,
                "event_type": "tool_call",
                "account_number": self.customer.account_number if self.customer else None,
                **entry,
            }
        )


# --- shared instruction fragments ------------------------------------------

VOICE_STYLE = """
VOICE
- Live phone call. One or two short sentences per reply, under 15 words each.
- Spoken words only: no markdown, emojis, asterisks, URLs.
- Say numbers as a person would: "order 8 8 2 1 3", "seventy one degrees".
- Measured pace. One question per turn, then stop and wait. Do not narrate.
"""

IDENTIFY_INSTRUCTIONS = (
    """
You are the HomeSense support assistant, an AI voice assistant. You are not a
person and have no human name.

Your only job is to identify the caller. Ask for the phone number OR the account
number on their account, whichever is easier.
- Phone number given: call identify_by_phone_number.
- Account number given: call identify_by_account_number. They sound like
  "H S 1 0 0 4 2".
Speech-to-text writes digits as words and mishears letters, so pass what you
heard straight through without correcting it. If a lookup fails, ask them to
repeat it slowly, try once more, then offer the other option.

Answer nothing about any account, order, device, subscription or policy until a
lookup succeeds.
"""
    + VOICE_STYLE
).strip()

SUPPORT_INSTRUCTIONS = (
    """
You are the HomeSense support assistant, an AI voice assistant for HomeSense
(thermostats, cameras, door locks, sensors, cloud video plan). Not a person, no
human name.

VERIFIED
This caller is already verified — by caller ID, signed-in web, account number or
phone number. NEVER ask for an account or phone number again; you have no tool
to look anyone up. A tool failure is not an identity problem: say what you could
not retrieve, then offer a ticket or a specialist.

TOOLS ARE YOUR ONLY SOURCE
- Account, device, temperature, order and subscription facts: from tools only.
- How-to, policy, plan, price, warranty, returns: from search_kb only, never
  memory. Mention the article title.
- Never invent a device, temperature, order, date, price or ticket number.
- Device lookups match loosely, so say the device name the tool returned. If it
  differs from their wording, say so and ask if that is the one.
- This caller's account only. If they name another, decline briefly, carry on.

HANDLING
- Resolve shorthand ("the other one", "that order") against earlier turns and
  confirm briefly. If unclear, ask one short question.
- Offline device or one needing a technician: stop troubleshooting, offer a
  ticket, then create_support_ticket and read back the number.
- Asked to remember something: add_account_note, then "Noted on your account."

ESCALATE with transfer_to_human, reason =
- requested: they ask for a person, manager or human.
- irate: angry or frustrated — repeating themselves, "ridiculous", "third time",
  swearing, threatening to cancel. Empathise ONCE, then transfer. Do not argue
  or keep troubleshooting.
- unresolved: two attempts at the same issue failed.
- policy: refund, cancellation, billing, anything involving money. You cannot do
  these; promise no outcome.

Be warm, brief, efficient.
"""
    + VOICE_STYLE
).strip()


# --- read-only lookups (swapped for MCP tools when USE_MCP=1) ---------------
#
# These are module-level rather than methods so the tool list can be swapped
# wholesale for the MCP toolset. Ticketing, notes and transfer always stay
# local - they are the write path and the escalation path.


@function_tool()
async def get_recent_order_status(
    context: RunContext, account_number: str = ""
) -> dict[str, Any]:
    """Status of the caller's most recent order.

    Args:
        account_number: Omit unless the caller names another account.
    """
    state: CallState = context.userdata
    customer = state.require_scope(account_number)

    # Real order systems are slow; hold the floor with a filler line if the
    # lookup makes the caller wait more than a beat.
    async with context.with_filler("Let me pull that up.", delay=0.4):
        order = backend.get_recent_order(customer)

    if order is None:
        state.note_tool("get_recent_order_status", {"result": "no_orders"})
        raise ToolError(f"Account {customer.account_number} has no orders on file.")

    state.note_tool(
        "get_recent_order_status",
        {"result": "ok", "order_id": order["order_id"], "status": order["status"]},
    )
    return {
        "order_id": order["order_id"],
        "status": order["status"],
        "items": order.get("items", []),
        "carrier": order.get("carrier"),
        "estimated_delivery": order.get("estimated_delivery"),
        "eta_days": order.get("eta_days"),
        "delivered_on": order.get("delivered_on"),
        "spoken_summary": backend.spoken_order_status(order),
    }


@function_tool()
async def get_device_status(
    context: RunContext, device_name: str, account_number: str = ""
) -> dict[str, Any]:
    """Check whether one of the caller's devices is active, online or offline.

    Args:
        device_name: The device as the caller described it.
        account_number: Omit unless the caller names another account.
    """
    state: CallState = context.userdata
    customer = state.require_scope(account_number)

    async with context.with_filler("Let me check that.", delay=0.4):
        try:
            device = backend.get_device_status(customer, device_name)
        except BackendError as exc:
            state.note_tool("get_device_status", {"input": device_name, "result": "not_found"})
            raise ToolError(str(exc)) from exc

    state.note_tool(
        "get_device_status",
        {
            "input": device_name,
            "result": "ok",
            "device_id": device["device_id"],
            "status": device["status"],
        },
    )
    return {
        "device_id": device["device_id"],
        "name": device["name"],
        "type": device["type"],
        "room": device["room"],
        "status": device["status"],
        "last_seen": device.get("last_seen"),
        "battery_pct": device.get("battery_pct"),
        "temperature_f": device.get("temperature_f"),
        "note": device.get("note"),
    }


@function_tool()
async def get_room_temperature(
    context: RunContext, room: str, account_number: str = ""
) -> dict[str, Any]:
    """Current temperature from the thermostat in a room.

    Args:
        room: The room the caller named.
        account_number: Omit unless the caller names another account.
    """
    state: CallState = context.userdata
    customer = state.require_scope(account_number)

    try:
        reading = backend.get_temperature(customer, room)
    except BackendError as exc:
        state.note_tool("get_room_temperature", {"input": room, "result": "not_found"})
        raise ToolError(str(exc)) from exc

    state.note_tool(
        "get_room_temperature",
        {"input": room, "result": "ok", "temperature_f": reading["temperature_f"]},
    )
    return reading


@function_tool()
async def get_subscription_status(
    context: RunContext, account_number: str = ""
) -> dict[str, Any]:
    """The caller's subscription plan, status and renewal date.

    Args:
        account_number: Omit unless the caller names another account.
    """
    state: CallState = context.userdata
    customer = state.require_scope(account_number)

    subscription = backend.get_subscription_status(customer)
    state.note_tool(
        "get_subscription_status", {"result": "ok", "status": subscription.get("status")}
    )
    return subscription


@function_tool()
async def list_registered_devices(
    context: RunContext, account_number: str = ""
) -> dict[str, Any]:
    """List every device registered to the caller's account.

    Args:
        account_number: Omit unless the caller names another account.
    """
    state: CallState = context.userdata
    customer = state.require_scope(account_number)

    devices = backend.list_devices(customer)
    state.note_tool("list_registered_devices", {"result": "ok", "count": len(devices)})
    return {"devices": devices, "count": len(devices)}


@function_tool()
async def search_kb(context: RunContext, query: str) -> dict[str, Any]:
    """Search the knowledge base: how-to, troubleshooting, plan, pricing,
    returns, warranty. Never answer these from memory.

    Args:
        query: The caller's question in their own words.
    """
    state: CallState = context.userdata

    async with context.with_filler("Let me look that up.", delay=0.4):
        articles = backend.search_kb(query)

    if not articles:
        state.note_tool("search_kb", {"query": query, "result": "no_match"})
        raise ToolError(
            f"Nothing in the knowledge base matches '{query}'. Tell the caller you don't "
            "have that documented and offer to create a ticket or pass them to a human."
        )

    titles = [a["title"] for a in articles]
    state.kb_articles.extend(t for t in titles if t not in state.kb_articles)
    state.note_tool("search_kb", {"query": query, "result": "ok", "titles": titles})
    return {"articles": articles, "count": len(articles)}


LOCAL_LOOKUP_TOOLS = [
    get_recent_order_status,
    get_device_status,
    get_room_temperature,
    get_subscription_status,
    list_registered_devices,
    search_kb,
]


# Exactly which MCP tools the model may see. The server also publishes
# `lookup_by_account` and `create_ticket`; both are deliberately withheld:
#
#   lookup_by_account - takes an arbitrary account number, so exposing it would
#       let the model pull up ANY customer, straight past require_scope. That is
#       a cross-account data leak, and allowed_tools is what prevents it.
#   create_ticket     - a second write path that would bypass CRM logging and
#       state.tickets, so tickets would not reach the human console or report.
#       Ticketing stays on the local tool.
#
# Withholding is enforced client-side by allowed_tools AND server-side by the
# account scope header, so neither alone is load-bearing.
ALLOWED_MCP_TOOLS = [
    "get_recent_order",
    "get_device_status",
    "get_temperature",
    "get_subscription",
    "search_kb",
]


def build_lookup_tools(account_number: str = "") -> list[Any]:
    """Read-only lookups: over MCP when USE_MCP=1, otherwise in-process.

    The LLM sees an equivalent tool contract either way; only the transport and
    the trust boundary change.

    Over MCP the verified account travels in the `x-homesense-account` header,
    which the server enforces against whatever account argument the model sends.
    That keeps scope enforcement on the server side of the boundary rather than
    trusting the model to pass the right account.
    """
    if not USE_MCP:
        return list(LOCAL_LOOKUP_TOOLS)

    headers = {"Authorization": f"Bearer {MCP_TOKEN}"}
    if account_number:
        headers["X-HomeSense-Account"] = account_number

    server_http = lk_mcp.MCPServerHTTP(
        url=MCP_URL,
        transport_type="streamable_http",
        headers=headers,
        allowed_tools=ALLOWED_MCP_TOOLS,
    )
    return [lk_mcp.MCPToolset(id="homesense-backend", mcp_server=server_http)]


class IdentifyAgent(Agent):
    """Phase one: greet the caller and verify who they are.

    Kept separate from SupportAgent so the identity gate is structural rather
    than a rule the model is asked to remember: this agent simply has no account
    tools to call.
    """

    def __init__(self, chat_ctx: agents.ChatContext | None = None) -> None:
        super().__init__(instructions=IDENTIFY_INSTRUCTIONS, chat_ctx=chat_ctx)

    async def on_enter(self) -> None:
        state: CallState = self.session.userdata
        state.phase = "identify"
        backend.crm_log(
            {
                "session_id": state.session_id,
                "event_type": "call_started",
                "channel": state.channel,
                "account_number": None,
                "identified_via": None,
            }
        )
        await self.session.generate_reply(
            instructions=(
                "Say exactly this and nothing more: \"Hi, you've reached HomeSense "
                "support. To pull up your account, what's the phone number or account "
                "number on file?\""
            )
        )

    @function_tool()
    async def identify_by_phone_number(
        self, context: RunContext, phone_number: str
    ) -> Agent:
        """Look up the caller by the phone number on their account.

        Args:
            phone_number: The number exactly as the caller said it.
        """
        state: CallState = context.userdata
        customer = backend.lookup_by_phone(phone_number)

        if customer is None:
            state.note_tool(
                "identify_by_phone_number",
                {"input": phone_number, "result": "not_found"},
            )
            raise ToolError(
                f"No HomeSense account has the phone number '{phone_number}'. Ask them "
                "to read it again, or offer to look them up by account number instead."
            )

        state.customer = customer
        state.identified_via = "phone_number"
        state.note_tool(
            "identify_by_phone_number",
            {
                "input": phone_number,
                "result": "found",
                "account_number": customer.account_number,
            },
        )
        logger.info(
            "HANDOFF IdentifyAgent → SupportAgent (%s via phone)", customer.account_number
        )
        state.emit("handoff", **{"from": "IdentifyAgent", "to": "SupportAgent", "via": "phone_number"})
        return SupportAgent(
            chat_ctx=self.chat_ctx,
            greet_by_name=True,
            account_number=customer.account_number,
        )

    @function_tool()
    async def identify_by_account_number(
        self, context: RunContext, account_number: str
    ) -> Agent:
        """Look up the caller by the account number they read out.

        Args:
            account_number: The account number exactly as the caller said it.
        """
        state: CallState = context.userdata
        customer = backend.lookup_by_account(account_number)

        if customer is None:
            state.note_tool(
                "identify_by_account_number",
                {"input": account_number, "result": "not_found"},
            )
            raise ToolError(
                f"No HomeSense account matches '{account_number}'. Ask the caller to "
                "read it again slowly, one character at a time."
            )

        state.customer = customer
        state.identified_via = "account_number"
        state.note_tool(
            "identify_by_account_number",
            {
                "input": account_number,
                "result": "found",
                "account_number": customer.account_number,
            },
        )
        logger.info("HANDOFF IdentifyAgent → SupportAgent (%s)", customer.account_number)
        state.emit(
            "handoff", **{"from": "IdentifyAgent", "to": "SupportAgent", "via": "account_number"}
        )

        # Returning an Agent is the documented handoff: the new agent takes over
        # the same session, carrying the conversation so far.
        return SupportAgent(
            chat_ctx=self.chat_ctx,
            greet_by_name=True,
            account_number=customer.account_number,
        )


class SupportAgent(Agent):
    """Phase two: answer questions, file tickets, escalate."""

    def __init__(
        self,
        chat_ctx: agents.ChatContext | None = None,
        *,
        greet_by_name: bool = False,
        introduce: bool = False,
        resumed: bool = False,
        account_number: str = "",
    ) -> None:
        super().__init__(
            instructions=SUPPORT_INSTRUCTIONS,
            chat_ctx=chat_ctx,
            # In MCP mode the verified account is baked into the connection
            # headers here, which is why this agent is only ever constructed
            # after identification.
            tools=build_lookup_tools(account_number),
        )
        self._greet_by_name = greet_by_name
        # True only when this agent speaks first (a phone caller matched by
        # number). After an IdentifyAgent handoff we have already introduced
        # ourselves, and saying it twice sounds robotic.
        self._introduce = introduce
        self._resumed = resumed

    # -- lifecycle ----------------------------------------------------------

    async def on_enter(self) -> None:
        state: CallState = self.session.userdata
        state.phase = "support"
        customer = state.customer

        if self._resumed:
            await self.session.generate_reply(
                instructions=(
                    "Say exactly this and nothing more: \"Sorry about that — picking up "
                    "where we left off.\""
                )
            )
            return

        if not self._greet_by_name:
            return

        assert customer is not None
        backend.crm_log(
            {
                "session_id": state.session_id,
                "event_type": "identified",
                "channel": state.channel,
                "account_number": customer.account_number,
                "identified_via": state.identified_via,
            }
        )
        await self.session.generate_reply(
            instructions=(
                (
                    f"Say exactly this and nothing more: \"Hi {customer.preferred_name}, "
                    "HomeSense support assistant here — what can I help with?\""
                    if self._introduce
                    else
                    f"Greet {customer.preferred_name} by name and ask how you can help, "
                    "in ONE short sentence. Do not introduce yourself again."
                )
                + " Do not mention anything about their account."
            )
        )

    # -- write path: always local, never MCP --------------------------------

    @function_tool()
    async def create_support_ticket(
        self,
        context: RunContext,
        issue_summary: str,
        device_id: str = "",
        priority: str = "normal",
        account_number: str = "",
    ) -> dict[str, Any]:
        """File a support ticket, once the caller agrees, then read back the number.

        Args:
            issue_summary: The problem and what was tried, for the technician.
            device_id: Affected device ID, if a device tool returned one.
            priority: "low", "normal" or "high".
            account_number: Omit unless the caller names another account.
        """
        state: CallState = context.userdata
        customer = state.require_scope(account_number)

        async with context.with_filler("Okay, I'm creating that ticket now.", delay=0.4):
            ticket = backend.create_ticket(
                account_number=customer.account_number,
                customer_name=customer.name,
                issue_summary=issue_summary,
                device_id=device_id or None,
                priority=priority,
                session_id=state.session_id,
            )

        state.tickets.append(ticket["ticket_id"])
        state.note_tool(
            "create_support_ticket",
            {
                "result": "ok",
                "ticket_id": ticket["ticket_id"],
                "issue_summary": issue_summary,
                "device_id": device_id,
                "priority": priority,
            },
        )
        return {
            "ticket_id": ticket["ticket_id"],
            "status": ticket["status"],
            "priority": ticket["priority"],
            "spoken_ticket_number": " ".join(ticket["ticket_id"].split("-")[-1]),
        }

    @function_tool()
    async def add_account_note(
        self, context: RunContext, note: str, account_number: str = ""
    ) -> dict[str, Any]:
        """Save a note on the caller's account when they ask you to remember something.

        Args:
            note: What to record, in one sentence.
            account_number: Omit unless the caller names another account.
        """
        state: CallState = context.userdata
        customer = state.require_scope(account_number)

        record = backend.add_account_note(
            account_number=customer.account_number,
            customer_name=customer.name,
            note=note,
            session_id=state.session_id,
        )
        state.notes.append(note)
        state.note_tool("add_account_note", {"result": "ok", "note": note})
        return {
            "note_id": record["note_id"],
            "confirmation": "Noted on your account.",
        }

    # -- escalation ---------------------------------------------------------

    @function_tool()
    async def transfer_to_human(
        self, context: RunContext, reason: EscalationReason, detail: str = ""
    ) -> str:
        """Hand the call to a human specialist with a summary.

        Args:
            reason: requested, irate, unresolved or policy.
            detail: One short sentence of context for the human.
        """
        state: CallState = context.userdata
        session = context.session

        state.escalation_reason = reason
        logger.info("ESCALATION reason=%s detail=%s", reason, detail or "-")
        state.emit("escalation", reason=reason, detail=detail)

        # (1) Build the structured handoff summary from the transcript so far.
        #     This is what makes it a *warm* transfer.
        summary = await self._summarise_call(session, state, reason, detail)

        # (2) Tell the caller what is happening before anything else moves.
        await session.say(
            "I'm connecting you to a specialist now. They'll have the full context, "
            "so you won't have to repeat yourself."
        )

        # (3) Persist for the CRM and the human's queue.
        handoff = backend.record_handoff(
            account_number=state.customer.account_number if state.customer else None,
            customer_name=state.customer.name if state.customer else None,
            summary=summary,
            reason=f"{reason}: {detail}" if detail else reason,
            mode="sip_warm_transfer" if self._sip_transfer_configured else "simulated_console",
            session_id=state.session_id,
            ticket_id=state.tickets[-1] if state.tickets else None,
            transcript=_transcript_items(session),
        )
        state.handoff = handoff
        state.note_tool(
            "transfer_to_human",
            {"result": "initiated", "reason": reason, "handoff_id": handoff["handoff_id"]},
        )
        state.emit(
            "transfer",
            reason=reason,
            mode=handoff["mode"],
            handoff_id=handoff["handoff_id"],
            ticket_id=handoff.get("ticket_id"),
        )

        # (4) Branch: real SIP warm transfer, or the simulated human console.
        if self._sip_transfer_configured:
            logger.info(
                "TRANSFER MODE: SIP warm transfer -> %s (trunk %s)",
                TRANSFER_PHONE_NUMBER,
                SIP_OUTBOUND_TRUNK_ID,
            )
            return await self._sip_warm_transfer(context, state, summary, handoff)

        logger.info("TRANSFER MODE: simulated human console (no SIP env vars set)")
        _print_handoff_block(handoff)
        return (
            "The specialist has the summary and is picking up now. "
            "Say a brief goodbye and thank the caller."
        )

    # -- transfer internals -------------------------------------------------

    @property
    def _sip_transfer_configured(self) -> bool:
        return bool(TRANSFER_PHONE_NUMBER and SIP_OUTBOUND_TRUNK_ID)

    async def _summarise_call(
        self, session: AgentSession, state: CallState, reason: str, detail: str
    ) -> str:
        """Generate the handoff summary from the conversation so far.

        Uses the session LLM directly rather than session.generate_reply so the
        summary is never spoken to the caller - it is for the human agent.
        """
        transcript = _transcript_text(session)
        identity = (
            f"{state.customer.name}, account {state.customer.account_number}"
            if state.customer
            else "an unidentified caller"
        )

        prompt = (
            "Summarise this customer service call for the human agent taking over. "
            "Use exactly these labels, one per line, no markdown:\n"
            "CALLER: name and account\n"
            "ASKED: what they wanted, one line\n"
            "ANSWERED: what was checked or told to them, one line\n"
            "OPEN ISSUE: what is still unresolved, one line\n"
            "TICKET: ticket number, or 'none'\n"
            "SENTIMENT: calm, frustrated or angry, plus a few words of evidence\n"
            "NEXT ACTION: what the human should do first, one line\n\n"
            f"Caller: {identity}\n"
            f"Escalation reason: {reason}\n"
            f"Extra context: {detail or 'none'}\n"
            f"Tickets filed this call: {', '.join(state.tickets) or 'none'}\n"
            f"Knowledge base articles used: {', '.join(state.kb_articles) or 'none'}\n\n"
            f"Transcript:\n{transcript}"
        )

        try:
            chat_ctx = agents.ChatContext.empty()
            chat_ctx.add_message(role="user", content=prompt)
            chunks: list[str] = []
            async with session.llm.chat(chat_ctx=chat_ctx) as stream:
                async for chunk in stream:
                    if chunk.delta and chunk.delta.content:
                        chunks.append(chunk.delta.content)
            summary = "".join(chunks).strip()
            if summary:
                return summary
        except Exception:
            logger.exception("summary generation failed; falling back to transcript digest")

        # Never block the transfer on the summariser - degrade to raw context.
        return (
            f"CALLER: {identity}\n"
            f"ASKED: see transcript\n"
            f"ANSWERED: see transcript\n"
            f"OPEN ISSUE: unknown (auto-summary unavailable)\n"
            f"TICKET: {', '.join(state.tickets) or 'none'}\n"
            f"SENTIMENT: unknown\n"
            f"NEXT ACTION: read the transcript below\n\n"
            f"{transcript[-1200:]}"
        )

    async def _sip_warm_transfer(
        self,
        context: RunContext,
        state: CallState,
        summary: str,
        handoff: dict[str, Any],
    ) -> str:
        """Dial the human into the room, brief them, then step out.

        This is the in-room variant of a warm transfer: the specialist joins the
        live call, the agent speaks the summary, then the agent leaves the two
        of them connected. It needs an outbound SIP trunk.
        """
        session = context.session
        ctx = agents.get_job_context()

        try:
            await ctx.add_sip_participant(
                call_to=TRANSFER_PHONE_NUMBER,
                trunk_id=SIP_OUTBOUND_TRUNK_ID,
                participant_identity="human-specialist",
                participant_name="HomeSense Specialist",
            )
        except Exception as exc:
            logger.exception("SIP dial failed; falling back to console handoff")
            _print_handoff_block(handoff)
            state.note_tool("transfer_to_human", {"result": "sip_failed", "error": str(exc)})
            raise ToolError(
                "I could not reach a specialist on the line. Tell the caller a specialist "
                "will call them back shortly, and that the summary has been passed along."
            ) from exc

        await session.say(f"Specialist, here's the context. {summary}")
        state.note_tool("transfer_to_human", {"result": "sip_connected"})
        # Keep the room alive: the caller and the specialist are still on it.
        state.sip_transfer_active = True
        logger.info("warm transfer complete, agent leaving room %s", state.room_name)

        ctx.shutdown(reason="warm transfer complete")
        return "The specialist is on the line and briefed. Do not say anything further."


# ─── 4. ENTRYPOINT (fresh OS process per call) ──────────────────────────────
def _has_subscribed_audio(room: Any) -> bool:
    """True once at least one remote participant's audio track is subscribed."""
    for participant in room.remote_participants.values():
        for publication in participant.track_publications.values():
            if publication.kind == rtc.TrackKind.KIND_AUDIO and publication.subscribed:
                return True
    return False


async def wait_for_audio_ready(
    ctx: JobContext,
    *,
    timeout: float = TRACK_WAIT_SECONDS,
    settle: float = GREETING_SETTLE_SECONDS,
) -> bool:
    """Block until the caller's audio is subscribed, then pause to settle.

    The SDK has no wait_for_track_subscription, so this listens for
    track_subscribed and also checks what is already subscribed - the track can
    land before we attach the handler.

    Returns True if audio became ready, False on timeout (we greet anyway
    rather than leaving the caller in silence).
    """
    if _has_subscribed_audio(ctx.room):
        await asyncio.sleep(settle)
        return True

    ready = asyncio.Event()

    def _on_subscribed(track: Any, publication: Any, participant: Any) -> None:
        if getattr(track, "kind", None) == rtc.TrackKind.KIND_AUDIO:
            ready.set()

    ctx.room.on("track_subscribed", _on_subscribed)
    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout)
        await asyncio.sleep(settle)
        return True
    except asyncio.TimeoutError:
        logger.info("no caller audio within %.1fs; greeting anyway", timeout)
        return False
    finally:
        ctx.room.off("track_subscribed", _on_subscribed)


def _caller_identity(ctx: JobContext) -> str | None:
    """Identity of the first remote participant, recorded on checkpoints."""
    for participant in ctx.room.remote_participants.values():
        if participant.identity:
            return str(participant.identity)
    return None


def is_phone_call(ctx: JobContext) -> bool:
    """True when this job is an inbound SIP call rather than a web session.

    Checked two ways because either can be available first: the dispatch rule
    names phone rooms with a "call-" prefix, and SIP participants carry
    `sip.*` attributes.
    """
    if (ctx.room.name or "").startswith(SIP_ROOM_PREFIX):
        return True
    return any(
        any(k.startswith("sip.") for k in p.attributes)
        for p in ctx.room.remote_participants.values()
    )


def parse_web_identity(
    metadata: str | None, attributes: dict[str, str] | None = None
) -> dict[str, str]:
    """Pull {account, phone} out of a web participant's metadata/attributes.

    The Next.js token route stamps the page's ?account= / ?phone= query onto the
    participant, as a stand-in for a real signed-in session. Attributes win over
    metadata when both are present, since they are the more explicit channel.

    Returns {} when there is nothing usable - malformed JSON is not an error
    worth failing a call over, it just means "not identified".
    """
    identity: dict[str, str] = {}

    if metadata:
        try:
            parsed = json.loads(metadata)
            if isinstance(parsed, dict):
                for key in ("account", "phone"):
                    value = parsed.get(key) or parsed.get(f"{key}_number")
                    if value and isinstance(value, (str, int)):
                        identity[key] = str(value).strip()
        except (json.JSONDecodeError, TypeError):
            pass

    for key, attr in (("account", "homesense.account"), ("phone", "homesense.phone")):
        value = (attributes or {}).get(attr)
        if value:
            identity[key] = str(value).strip()

    return {k: v for k, v in identity.items() if v}


def resolve_web_identity(ctx: JobContext) -> dict[str, str]:
    """Web identity for the first remote participant that carries one."""
    for participant in ctx.room.remote_participants.values():
        identity = parse_web_identity(
            getattr(participant, "metadata", None), dict(participant.attributes or {})
        )
        if identity:
            return identity
    return {}


def lookup_web_identity(identity: dict[str, str]) -> Customer | None:
    """Resolve a web identity to a customer, account number first."""
    account = identity.get("account")
    if account:
        customer = backend.lookup_by_account(account)
        if customer:
            return customer
    phone = identity.get("phone")
    if phone:
        return backend.lookup_by_phone(phone)
    return None


def resolve_caller_phone(ctx: JobContext) -> str | None:
    """Find the caller's number for an inbound SIP call.

    Sources, in order of reliability:
      1. the SIP participant's `sip.phoneNumber` attribute (the caller's number)
      2. `sip.trunkPhoneNumber` as a fallback (the number they dialled)
      3. job metadata, when a dispatch carries {"phone": "+1..."}
    """
    for participant in ctx.room.remote_participants.values():
        phone = participant.attributes.get("sip.phoneNumber")
        if phone:
            return phone

    for participant in ctx.room.remote_participants.values():
        phone = participant.attributes.get("sip.trunkPhoneNumber")
        if phone:
            logger.info("no sip.phoneNumber; falling back to sip.trunkPhoneNumber")
            return phone

    metadata = (ctx.job.metadata or "").strip()
    if metadata:
        try:
            parsed = json.loads(metadata)
            phone = parsed.get("phone") or parsed.get("phone_number") or parsed.get("from")
            if phone:
                return str(phone)
        except (json.JSONDecodeError, AttributeError):
            if metadata.startswith("+") or metadata.replace("-", "").isdigit():
                return metadata

    return None


def decide_away_action(state: CallState) -> tuple[str, str]:
    """Decide what to do when the caller has gone quiet.

    First silence gets a nudge; the second ends the call politely rather than
    holding an empty line open (and burning STT) indefinitely.

    Returns (action, payload) where action is "prompt" or "close".
    """
    state.away_prompts += 1
    if state.away_prompts == 1:
        name = state.customer.preferred_name if state.customer else "there"
        return "prompt", f"Are you still there, {name}?"
    return "close", "no response"


def goodbye_line(state: CallState) -> str:
    """Closing line, mentioning saved work only when there is some."""
    saved = (
        "your ticket and notes are saved" if state.tickets or state.notes else "nothing is lost"
    )
    return f"I'll close this for now — {saved}. Goodbye."


def restore_chat_ctx(snapshot: dict[str, Any]) -> agents.ChatContext:
    """Rebuild a ChatContext from a checkpoint's stored turns."""
    chat_ctx = agents.ChatContext.empty()
    for item in snapshot.get("chat_items", []):
        role = item.get("role")
        text = item.get("text")
        if role in ("user", "assistant") and text:
            chat_ctx.add_message(role=role, content=text)
    return chat_ctx


class TurnLatency:
    """Folds the per-component metric events into one line per conversational turn.

    Metrics arrive separately (EOU, LLM, TTS) and are correlated by `speech_id`.
    STT metrics have no speech_id - they are emitted per user utterance - so the
    most recent STT duration is carried forward.

    The number that matters to a caller is:

        eou (silence -> we decide they're done)
      + llm_ttft (-> first token back)
      + tts_ttfb (-> first byte of audio)
      = how long the line was quiet before the agent started speaking
    """

    def __init__(self, on_turn: Callable[[dict[str, float]], None] | None = None) -> None:
        self._turns: dict[str, dict[str, float]] = {}
        self._last_stt_ms: float | None = None
        self.turn_count = 0
        self._on_turn = on_turn

    @staticmethod
    def _ms(seconds: float | None) -> float | None:
        return round(seconds * 1000, 1) if seconds is not None else None

    def handle(self, m: Any) -> None:
        if isinstance(m, lk_metrics.STTMetrics):
            self._last_stt_ms = self._ms(m.duration)
            return

        speech_id = getattr(m, "speech_id", None)
        if not speech_id:
            return
        bucket = self._turns.setdefault(speech_id, {})

        if isinstance(m, lk_metrics.EOUMetrics):
            bucket["eou"] = self._ms(m.end_of_utterance_delay) or 0.0
            bucket["transcription"] = self._ms(m.transcription_delay) or 0.0
        elif isinstance(m, lk_metrics.LLMMetrics):
            if m.cancelled:  # discarded preemptive draft - not a real turn
                return
            bucket["llm_ttft"] = self._ms(m.ttft) or 0.0
            bucket["tokens"] = float(m.total_tokens or 0)
        elif isinstance(m, lk_metrics.TTSMetrics):
            if m.cancelled:
                return
            bucket.setdefault("tts_ttfb", self._ms(m.ttfb) or 0.0)

        if "llm_ttft" in bucket and "tts_ttfb" in bucket:
            self._emit(speech_id, self._turns.pop(speech_id))

    def _emit(self, speech_id: str, b: dict[str, float]) -> None:
        self.turn_count += 1
        eou = b.get("eou", 0.0)
        llm = b.get("llm_ttft", 0.0)
        tts = b.get("tts_ttfb", 0.0)
        stt = f"{self._last_stt_ms:.0f}ms" if self._last_stt_ms is not None else "-"
        if self._on_turn is not None:
            self._on_turn(
                {
                    "turn": float(self.turn_count),
                    "stt_ms": self._last_stt_ms or 0.0,
                    "eou_ms": eou,
                    "llm_ttft_ms": llm,
                    "tts_ttfb_ms": tts,
                    "e2e_ms": eou + llm + tts,
                    "tokens": b.get("tokens", 0.0),
                }
            )
        logger.info(
            "TURN %-2d stt=%s  eou=%.0fms  llm_ttft=%.0fms  tts_ttfb=%.0fms  "
            "e2e=%.0fms  tokens=%d",
            self.turn_count,
            stt,
            eou,
            llm,
            tts,
            eou + llm + tts,
            int(b.get("tokens", 0)),
        )


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    configure_logging()
    log_stack()

    state = CallState(
        session_id=ctx.job.id or "local-session", room_name=ctx.room.name or "console"
    )

    # 4a. Identify the caller before the first word is spoken.
    #     The participant may not have joined yet, so wait briefly for them -
    #     both caller ID and web session metadata live on the participant.
    try:
        await asyncio.wait_for(ctx.wait_for_participant(), timeout=PARTICIPANT_WAIT_SECONDS)
    except Exception:
        # Console mode has no remote participant at all, and a timeout just
        # means we identify the caller by asking. Never fail the call for this.
        logger.debug("no remote participant within %.1fs", PARTICIPANT_WAIT_SECONDS)

    phone_call = is_phone_call(ctx)
    state.channel = "phone" if phone_call else "web"
    logger.info("CHANNEL %s  room=%s", state.channel, state.room_name)

    phone = resolve_caller_phone(ctx)
    if phone:
        state.caller_phone = phone
        customer = backend.lookup_by_phone(phone)
        if customer:
            state.customer = customer
            state.identified_via = "caller_id"
            logger.info("CALLER %s -> %s (%s)", phone, customer.name, customer.account_number)
        else:
            logger.info("CALLER %s -> no matching account, will ask for account number", phone)
    elif phone_call:
        logger.info("SIP call with no caller ID; will ask for account number")

    # An already-signed-in web caller: the page forwarded ?account= / ?phone=
    # and the token route stamped it onto the participant. Same outcome as a
    # caller-ID match - straight to SupportAgent, greeted by name.
    if not state.identified:
        web_identity = resolve_web_identity(ctx)
        if web_identity:
            customer = lookup_web_identity(web_identity)
            if customer:
                state.customer = customer
                state.identified_via = "web_session"
                logger.info(
                    "IDENTIFIED via web session metadata account=%s -> %s",
                    customer.account_number,
                    customer.name,
                )
            else:
                logger.info(
                    "web session metadata %s matched no account; will ask for account number",
                    web_identity,
                )

    state.emit(
        "call_started",
        channel=state.channel,
        identified_via=state.identified_via,
        customer=state.customer.preferred_name if state.customer else None,
    )

    # 4b. Crash resilience: if this room was mid-call when a previous process
    #     died, pick up where it left off instead of starting over.
    #     A checkpoint older than CHECKPOINT_MAX_AGE_SECONDS is ignored, so a
    #     recycled room name cannot resurrect a stranger's conversation.
    snapshot = backend.load_checkpoint(state.room_name)
    resume_ctx: agents.ChatContext | None = None
    if snapshot and snapshot.get("account_number"):
        restored = backend.lookup_by_account(snapshot["account_number"])
        if restored:
            state.customer = restored
            state.tickets = list(snapshot.get("tickets") or [])
            state.rehydrated = True
            resume_ctx = restore_chat_ctx(snapshot)
            logger.info(
                "REHYDRATED from checkpoint  room=%s  customer=%s (%s)  turns=%d  "
                "age=%.0fs  prev_job=%s",
                state.room_name,
                restored.name,
                restored.account_number,
                len(snapshot.get("chat_items", [])),
                backend.checkpoint_age_seconds(snapshot),
                snapshot.get("job_id") or "unknown",
            )
    elif snapshot is None and backend.checkpoint_path(state.room_name).exists():
        logger.info("stale checkpoint ignored for room=%s", state.room_name)

    # 4c. Build the session. Models are LiveKit Inference strings, so no
    #     provider API keys are needed - just LIVEKIT_* credentials.
    session: AgentSession[CallState] = AgentSession(
        stt=STT_MODEL,
        llm=LLM_MODEL,
        tts=build_tts(),
        vad=ctx.proc.userdata.get("vad"),
        userdata=state,
        user_away_timeout=USER_AWAY_TIMEOUT,
        turn_handling=TurnHandlingOptions(
            # Semantic end-of-turn: waits on meaning, not just silence.
            turn_detection=inference.TurnDetector(),
            # Start drafting the reply before the caller finishes, and throw it
            # away if they keep talking. Biggest single latency win.
            preemptive_generation=PreemptiveGenerationOptions(enabled=True),
            endpointing={
                "mode": "dynamic",
                # Floor keeps slow account-number dictation ("H... S... one...
                # zero...") from being chopped into separate turns; the turn
                # detector is what actually decides, this is just the window.
                # The ceiling was 6.0s and calls were hitting it: five turns in
                # one session sat at 6001ms of pure dead air after a SHORT
                # utterance, making 7s round trips.
                "min_delay": float(os.getenv("ENDPOINTING_MIN_DELAY", "0.5")),
                "max_delay": float(os.getenv("ENDPOINTING_MAX_DELAY", "2.5")),
            },
            interruption={
                "enabled": True,
                "mode": "adaptive",
                # A backchannel ("mhm", "okay") should not stop the agent; a
                # real sentence should.
                "min_duration": 0.5,
                "min_words": 2,
                "resume_false_interruption": True,
                "false_interruption_timeout": 2.0,
            },
        ),
    )

    # Confirm the latency-critical options actually took effect - a typo in a
    # TypedDict key is silently ignored, so log what resolved, not what we set.
    _opts = session.options
    logger.info(
        "TURN OPTS preemptive_generation=%s  endpointing=%s..%ss (%s)  interruptions=%s",
        _opts.preemptive_generation.get("enabled"),
        _opts.endpointing.get("min_delay"),
        _opts.endpointing.get("max_delay"),
        _opts.endpointing.get("mode"),
        _opts.interruption.get("enabled"),
    )

    # 4d. Observability: one latency line per turn, plus a usage tally.
    turn_latency = TurnLatency(on_turn=lambda m: state.emit("turn", **m))

    @session.on("metrics_collected")
    def _on_metrics(event: agents.MetricsCollectedEvent) -> None:
        try:
            turn_latency.handle(event.metrics)
        except Exception:
            logger.debug("turn latency aggregation failed", exc_info=True)

    # Barge-in: log when the caller actually cuts the agent off.
    @session.on("speech_created")
    def _on_speech(event: agents.SpeechCreatedEvent) -> None:
        handle = event.speech_handle

        def _done(h: Any) -> None:
            if h.interrupted:
                logger.info("INTERRUPTED (agent speech cut off by caller)")
                state.emit("interruption")

        handle.add_done_callback(_done)

    @session.on("agent_false_interruption")
    def _on_false_interruption(event: agents.AgentFalseInterruptionEvent) -> None:
        logger.info("false interruption ignored (resumed=%s)", event.resumed)

    # 4e. Mirror the transcript into the CRM as it happens, and checkpoint, so a
    #     dropped call still leaves a usable record and can be resumed.
    @session.on("conversation_item_added")
    def _on_item(event: agents.ConversationItemAddedEvent) -> None:
        item = event.item
        backend.crm_log(
            {
                "session_id": state.session_id,
                "event_type": "transcript",
                "role": getattr(item, "role", "unknown"),
                "text": _item_text(item),
                "account_number": state.customer.account_number if state.customer else None,
            }
        )
        role = getattr(item, "role", None)
        text = _item_text(item)
        if text and role in ("user", "assistant"):
            state.emit(
                "user_turn" if role == "user" else "agent_speech",
                text=text[:180],
            )

        # Best-effort snapshot; a checkpoint failure must never break the call.
        try:
            backend.save_checkpoint(
                room_name=state.room_name,
                account_number=state.customer.account_number if state.customer else None,
                customer_name=state.customer.name if state.customer else None,
                phase=state.phase,
                chat_items=_transcript_items(session),
                tickets=state.tickets,
                job_id=ctx.job.id,
                participant_identity=_caller_identity(ctx),
            )
        except Exception:
            logger.debug("checkpoint write failed", exc_info=True)

    # 4f. Silence handling: nudge once, then close politely.
    @session.on("user_state_changed")
    def _on_user_state(event: agents.UserStateChangedEvent) -> None:
        if event.new_state != "away":
            return
        action, payload = decide_away_action(state)
        logger.info("USER_AWAY n=%d action=%s", state.away_prompts, action)

        if action == "prompt":
            session.say(payload)
        else:
            asyncio.create_task(_close_politely(session, ctx, state, payload))

    # 4g. End of call: write the session report and close the CRM record.
    async def write_report() -> None:
        # session.usage supersedes the deprecated UsageCollector/UsageSummary
        # pair, and reports per model/provider rather than one flat total.
        try:
            logger.info("USAGE turns=%d%s", turn_latency.turn_count, _usage_line(session))
        except Exception:
            logger.debug("usage summary failed", exc_info=True)

        try:
            report = ctx.make_session_report(session).to_dict()
        except Exception:
            logger.exception("make_session_report failed; writing a minimal report")
            report = {}

        report.update(
            {
                "session_id": state.session_id,
                "room": state.room_name,
                "channel": state.channel,
                "caller_phone": state.caller_phone,
                "account_number": state.customer.account_number if state.customer else None,
                "customer_name": state.customer.name if state.customer else None,
                "identified": state.identified,
                "identified_via": state.identified_via,
                "rehydrated": state.rehydrated,
                "tools_via": "mcp" if USE_MCP else "local",
                "tool_calls": state.tool_calls,
                "tickets_created": state.tickets,
                "account_notes": state.notes,
                "kb_articles_used": state.kb_articles,
                "escalation_reason": state.escalation_reason,
                "handoff": state.handoff,
                "transcript": _transcript_items(session),
            }
        )
        path = backend.save_session_report(report, session_id=state.session_id)

        backend.crm_log(
            {
                "session_id": state.session_id,
                "event_type": "call_ended",
                "channel": state.channel,
                "account_number": state.customer.account_number if state.customer else None,
                "identified": state.identified,
                "tool_call_count": len(state.tool_calls),
                "tickets_created": state.tickets,
                "transferred": state.handoff is not None,
                "escalation_reason": state.escalation_reason,
                "report_path": str(path),
            }
        )
        state.emit(
            "session_end",
            channel=state.channel,
            identified=state.identified,
            identified_via=state.identified_via,
            tickets=len(state.tickets),
            transferred=state.handoff is not None,
            escalation_reason=state.escalation_reason,
            turns=turn_latency.turn_count,
        )
        logger.info("session report written to %s", path)

        # The call is over; the checkpoint has nothing left to resume.
        backend.clear_checkpoint(state.room_name)

        # LAST: everything above is already on disk, so releasing the room now
        # cannot cost us the report or the CRM rows.
        await close_room(ctx, state)

    ctx.add_shutdown_callback(write_report)

    # 4h. Hard stop so a wedged call cannot run forever.
    guard = asyncio.create_task(_max_duration_guard(session, ctx, state))
    ctx.add_shutdown_callback(lambda: _cancel(guard))

    # Phone caller already matched -> straight to support. Otherwise identify.
    if state.rehydrated:
        first_agent: Agent = SupportAgent(
            chat_ctx=resume_ctx,
            resumed=True,
            account_number=state.customer.account_number if state.customer else None,
        )
    elif state.identified:
        # Matched on caller ID, so this agent speaks the very first words.
        first_agent = SupportAgent(
            greet_by_name=True,
            introduce=True,
            account_number=state.customer.account_number if state.customer else None,
        )
    else:
        first_agent = IdentifyAgent()

    # Hold the greeting until the caller can actually hear it.
    await wait_for_audio_ready(ctx)

    await session.start(first_agent, room=ctx.room)


# ─── helpers ────────────────────────────────────────────────────────────────
async def close_room(ctx: JobContext, state: CallState) -> bool:
    """Delete the room once the call is finished and everything is written.

    Without this the room sits empty until the server's empty-room timeout, so
    LiveKit Cloud shows the session "active" for minutes after the caller hung
    up and the metrics finalise late.

    Not done via RoomOptions(delete_room_on_close=True): that fires on EVERY
    session close, including when the agent deliberately steps out of a SIP warm
    transfer — which would hang up on the caller and the human specialist
    mid-conversation. Returns True if the room was deleted.
    """
    if state.sip_transfer_active:
        logger.info("ROOM KEPT %s (warm transfer in progress)", state.room_name)
        return False
    try:
        await ctx.delete_room()
        logger.info("ROOM CLOSED %s", state.room_name)
        return True
    except Exception:  # noqa: BLE001 - never let cleanup fail a finished call
        logger.warning("could not delete room %s", state.room_name, exc_info=True)
        return False


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _close_politely(
    session: AgentSession, ctx: JobContext, state: CallState, reason: str
) -> None:
    """Say goodbye, then shut down so the report and CRM entries still get written."""
    logger.info("CLOSING reason=%s", reason)
    try:
        handle = session.say(goodbye_line(state))
        await handle.wait_for_playout()
    except Exception:
        logger.debug("goodbye playback failed", exc_info=True)
    ctx.shutdown(reason=reason)


async def _max_duration_guard(
    session: AgentSession, ctx: JobContext, state: CallState
) -> None:
    """End the call politely if it runs past the cap."""
    started = time.monotonic()
    try:
        await asyncio.sleep(MAX_SESSION_SECONDS)
    except asyncio.CancelledError:
        return
    logger.warning(
        "MAX DURATION reached (%.0fs) - closing", time.monotonic() - started
    )
    await _close_politely(session, ctx, state, "max session duration")


def _usage_line(session: AgentSession) -> str:
    """Compact per-model usage tail for the end-of-call log line."""
    parts = []
    for usage in session.usage.model_usage:
        name = f"{usage.provider}/{usage.model}"
        if isinstance(usage, lk_metrics.LLMModelUsage):
            parts.append(f"  {name} {usage.input_tokens}in/{usage.output_tokens}out")
        elif isinstance(usage, lk_metrics.TTSModelUsage):
            parts.append(f"  {name} {usage.characters_count}chars")
        elif isinstance(usage, lk_metrics.STTModelUsage):
            parts.append(f"  {name} {usage.audio_duration:.1f}s")
    return "".join(parts)


def _item_text(item: Any) -> str:
    """Flatten a ChatMessage's content list into plain text."""
    content = getattr(item, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = [c for c in content if isinstance(c, str)]
    return " ".join(parts).strip()


def _transcript_items(session: AgentSession) -> list[dict[str, Any]]:
    items = []
    for item in session.history.items:
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            continue
        text = _item_text(item)
        if text:
            items.append({"role": role, "text": text})
    return items


def _transcript_text(session: AgentSession) -> str:
    lines = [
        f"{'Caller' if i['role'] == 'user' else 'Agent'}: {i['text']}"
        for i in _transcript_items(session)
    ]
    return "\n".join(lines) if lines else "(no transcript captured)"


def _print_handoff_block(handoff: dict[str, Any]) -> None:
    """The 'human agent screen' for the demo when no SIP trunk is configured."""
    width = 78
    print("\n" + "=" * width, flush=True)
    print("  INCOMING WARM TRANSFER  ->  HUMAN SPECIALIST QUEUE".center(width), flush=True)
    print("=" * width, flush=True)
    print(f"  Handoff ID : {handoff['handoff_id']}", flush=True)
    print(f"  Customer   : {handoff.get('customer_name') or 'Unidentified'}", flush=True)
    print(f"  Account    : {handoff.get('account_number') or '-'}", flush=True)
    print(f"  Ticket     : {handoff.get('ticket_id') or '-'}", flush=True)
    print(f"  Reason     : {handoff.get('reason')}", flush=True)
    print("-" * width, flush=True)
    print("  CONTEXT SUMMARY", flush=True)
    for raw_line in (handoff.get("summary") or "").splitlines():
        for line in textwrap.wrap(raw_line, width=width - 4) or [""]:
            print(f"  {line}", flush=True)
    print("=" * width + "\n", flush=True)


# ─── 5. RUN ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    agents.cli.run_app(server)
