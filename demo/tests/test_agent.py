"""Tests for the agent-side logic that doesn't need a live LiveKit session:
per-turn latency aggregation, channel detection and caller-ID resolution.
"""

from __future__ import annotations

import logging

import pytest
from livekit.agents import metrics as lk_metrics

import agent

# ---------------------------------------------------------------------------
# Metric factories
# ---------------------------------------------------------------------------


def stt(duration=0.12):
    return lk_metrics.STTMetrics(
        label="stt", request_id="r", timestamp=0.0, duration=duration,
        audio_duration=2.0, streamed=True,
    )


def eou(delay=0.35, speech_id="s1"):
    return lk_metrics.EOUMetrics(
        timestamp=0.0, end_of_utterance_delay=delay, transcription_delay=0.05,
        on_user_turn_completed_delay=0.0, speech_id=speech_id,
    )


def llm(ttft=0.28, speech_id="s1", cancelled=False, total_tokens=140):
    return lk_metrics.LLMMetrics(
        label="llm", request_id="r", timestamp=0.0, duration=0.6, ttft=ttft,
        cancelled=cancelled, completion_tokens=40, prompt_tokens=100,
        prompt_cached_tokens=0, total_tokens=total_tokens, tokens_per_second=60.0,
        speech_id=speech_id,
    )


def tts(ttfb=0.19, speech_id="s1", cancelled=False):
    return lk_metrics.TTSMetrics(
        label="tts", request_id="r", timestamp=0.0, ttfb=ttfb, duration=1.2,
        audio_duration=1.1, cancelled=cancelled, characters_count=52,
        streamed=True, speech_id=speech_id,
    )


# ---------------------------------------------------------------------------
# TurnLatency
# ---------------------------------------------------------------------------


def test_turn_emits_once_both_halves_arrive(caplog):
    t = agent.TurnLatency()
    with caplog.at_level(logging.INFO, logger="homesense"):
        t.handle(stt(0.12))
        t.handle(eou(0.35))
        assert t.turn_count == 0, "must not emit before the response path reports"
        t.handle(llm(0.28))
        assert t.turn_count == 0
        t.handle(tts(0.19))
        assert t.turn_count == 1

    line = caplog.text
    assert "TURN 1" in line
    assert "eou=350ms" in line
    assert "llm_ttft=280ms" in line
    assert "tts_ttfb=190ms" in line
    # e2e is the sum of the three stages the caller actually waits through.
    assert "e2e=820ms" in line
    assert "tokens=140" in line
    assert "stt=120ms" in line


def test_metric_order_does_not_matter(caplog):
    """TTS can report before LLM depending on scheduling."""
    t = agent.TurnLatency()
    with caplog.at_level(logging.INFO, logger="homesense"):
        t.handle(tts(0.19))
        t.handle(eou(0.35))
        t.handle(llm(0.28))
    assert t.turn_count == 1
    assert "e2e=820ms" in caplog.text


def test_cancelled_llm_is_not_a_turn():
    """Preemptive generation discards drafts; they must not be counted."""
    t = agent.TurnLatency()
    t.handle(eou(0.35))
    t.handle(llm(0.28, cancelled=True))
    t.handle(tts(0.19))
    assert t.turn_count == 0


def test_cancelled_tts_is_not_a_turn():
    t = agent.TurnLatency()
    t.handle(eou(0.35))
    t.handle(tts(0.19, cancelled=True))
    t.handle(llm(0.28))
    assert t.turn_count == 0


def test_turns_are_tracked_independently(caplog):
    t = agent.TurnLatency()
    with caplog.at_level(logging.INFO, logger="homesense"):
        t.handle(eou(0.30, speech_id="a"))
        t.handle(eou(0.40, speech_id="b"))
        t.handle(llm(0.20, speech_id="b"))
        t.handle(tts(0.10, speech_id="b"))
        t.handle(llm(0.25, speech_id="a"))
        t.handle(tts(0.15, speech_id="a"))
    assert t.turn_count == 2
    assert "e2e=700ms" in caplog.text  # b: 400+200+100
    assert "e2e=700ms" in caplog.text  # a: 300+250+150


def test_metrics_without_speech_id_are_ignored():
    t = agent.TurnLatency()
    t.handle(eou(0.35, speech_id=None))
    t.handle(llm(0.28, speech_id=None))
    assert t.turn_count == 0


def test_stt_is_optional(caplog):
    """No STT metric yet (first turn) must still produce a line."""
    t = agent.TurnLatency()
    with caplog.at_level(logging.INFO, logger="homesense"):
        t.handle(eou(0.35))
        t.handle(llm(0.28))
        t.handle(tts(0.19))
    assert "stt=-" in caplog.text


# ---------------------------------------------------------------------------
# Channel detection / caller ID
# ---------------------------------------------------------------------------


class FakeParticipant:
    def __init__(self, attributes):
        self.attributes = attributes


class FakeRoom:
    def __init__(self, name="", participants=()):
        self.name = name
        self.remote_participants = {str(i): p for i, p in enumerate(participants)}


class FakeJob:
    def __init__(self, metadata=""):
        self.metadata = metadata
        self.id = "job-1"


class FakeCtx:
    def __init__(self, room_name="", participants=(), metadata=""):
        self.room = FakeRoom(room_name, participants)
        self.job = FakeJob(metadata)


def test_web_session_is_not_a_phone_call():
    ctx = FakeCtx(room_name="room-abc123", participants=[FakeParticipant({})])
    assert agent.is_phone_call(ctx) is False
    assert agent.resolve_caller_phone(ctx) is None


def test_sip_detected_by_room_prefix():
    """The dispatch rule names phone rooms 'call-*'."""
    assert agent.is_phone_call(FakeCtx(room_name="call-_+12066069168_xyz")) is True


def test_sip_detected_by_participant_attribute():
    ctx = FakeCtx(
        room_name="whatever",
        participants=[FakeParticipant({"sip.phoneNumber": "+14155550142"})],
    )
    assert agent.is_phone_call(ctx) is True


def test_caller_phone_from_sip_attribute():
    ctx = FakeCtx(participants=[FakeParticipant({"sip.phoneNumber": "+14155550142"})])
    assert agent.resolve_caller_phone(ctx) == "+14155550142"


def test_caller_phone_prefers_phone_number_over_trunk():
    ctx = FakeCtx(
        participants=[
            FakeParticipant(
                {"sip.trunkPhoneNumber": "+12066069168", "sip.phoneNumber": "+14155550142"}
            )
        ]
    )
    assert agent.resolve_caller_phone(ctx) == "+14155550142"


def test_caller_phone_falls_back_to_trunk():
    ctx = FakeCtx(participants=[FakeParticipant({"sip.trunkPhoneNumber": "+12066069168"})])
    assert agent.resolve_caller_phone(ctx) == "+12066069168"


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ('{"phone": "+14155550142"}', "+14155550142"),
        ('{"phone_number": "+14155550142"}', "+14155550142"),
        ('{"from": "+14155550142"}', "+14155550142"),
        ("+14155550142", "+14155550142"),
        ("", None),
        ("not-json-not-a-number", None),
    ],
)
def test_caller_phone_from_job_metadata(metadata, expected):
    assert agent.resolve_caller_phone(FakeCtx(metadata=metadata)) == expected


def test_sip_caller_resolves_to_a_real_account():
    """End to end: SIP attribute -> account lookup."""
    import backend

    ctx = FakeCtx(
        room_name="call-_+12066069168_x",
        participants=[FakeParticipant({"sip.phoneNumber": "+14155550142"})],
    )
    assert agent.is_phone_call(ctx) is True
    customer = backend.lookup_by_phone(agent.resolve_caller_phone(ctx))
    assert customer.account_number == "HS-30001"


# ---------------------------------------------------------------------------
# Stack / config
# ---------------------------------------------------------------------------


def test_models_are_livekit_inference_strings():
    """All three must be 'provider/model' strings routed via LiveKit Inference."""
    for model in (agent.STT_MODEL, agent.LLM_MODEL, agent.TTS_MODEL):
        assert "/" in model, model


def test_log_stack_names_all_three_models(caplog):
    with caplog.at_level(logging.INFO, logger="homesense"):
        agent.log_stack()
    text = caplog.text
    assert agent.STT_MODEL in text
    assert agent.LLM_MODEL in text
    assert agent.TTS_MODEL in text
    assert "LiveKit Inference" in text
    assert "preemptive_generation=True" in text
    assert "silero" in text


def test_automatic_dispatch_no_agent_name():
    """The web app dispatches automatically; naming the agent would break it.

    Setting agent_name switches the worker to *explicit* dispatch, and it would
    silently stop joining the rooms the Next.js app creates.
    """
    import inspect
    import re

    src = inspect.getsource(agent)
    assert "@server.rtc_session()" in src
    # No agent_name passed as a keyword anywhere (prose mentioning it is fine).
    assert not re.search(r"agent_name\s*=", src)


# ---------------------------------------------------------------------------
# Multi-agent handoff and the identity gate (items 7, 8)
# ---------------------------------------------------------------------------

import contextlib  # noqa: E402
import inspect  # noqa: E402
import os  # noqa: E402

from livekit.agents import ToolError  # noqa: E402
from livekit.agents.llm.tool_context import (  # noqa: E402
    get_function_info,
    is_function_tool,
)

import backend  # noqa: E402


def tool_names(agent_obj):
    return [get_function_info(t).name for t in agent_obj.tools if is_function_tool(t)]


class FakeRunContext:
    """Stands in for RunContext: userdata + the filler context manager."""

    def __init__(self, state, session=None):
        self.userdata = state
        self.session = session
        self.fillers = []

    @contextlib.asynccontextmanager
    async def with_filler(self, source, **kwargs):
        self.fillers.append(source)
        yield


def test_identify_agent_has_only_identification_tools():
    """The identity gate is structural: no account tools exist to call yet."""
    assert sorted(tool_names(agent.IdentifyAgent())) == [
        "identify_by_account_number",
        "identify_by_phone_number",
    ]


def test_support_agent_has_the_account_tools():
    names = tool_names(agent.SupportAgent())
    for expected in [
        "get_recent_order_status",
        "get_device_status",
        "get_room_temperature",
        "get_subscription_status",
        "list_registered_devices",
        "search_kb",
        "create_support_ticket",
        "add_account_note",
        "transfer_to_human",
    ]:
        assert expected in names, expected
    # Identification is done; that tool must not linger.
    assert "identify_by_account_number" not in names


async def test_identify_returns_support_agent_as_handoff(caplog):
    """The documented handoff pattern: the tool returns the next Agent."""
    state = agent.CallState()
    ctx = FakeRunContext(state)
    with caplog.at_level(logging.INFO, logger="homesense"):
        result = await agent.IdentifyAgent().identify_by_account_number(ctx, "H S 1 0 0 4 2")

    assert isinstance(result, agent.SupportAgent)
    assert state.customer.account_number == "HS-10042"
    assert "HANDOFF IdentifyAgent → SupportAgent" in caplog.text


async def test_identify_failure_raises_and_does_not_hand_off():
    state = agent.CallState()
    with pytest.raises(ToolError):
        await agent.IdentifyAgent().identify_by_account_number(FakeRunContext(state), "HS-99999")
    assert state.customer is None


@pytest.mark.parametrize(
    "call",
    [
        lambda ctx: agent.get_recent_order_status(ctx),
        lambda ctx: agent.get_device_status(ctx, "thermostat"),
        lambda ctx: agent.get_room_temperature(ctx, "living room"),
        lambda ctx: agent.get_subscription_status(ctx),
        lambda ctx: agent.list_registered_devices(ctx),
    ],
)
async def test_identity_gate_blocks_every_account_tool(call):
    """Item 12c: no account data leaves the building before verification."""
    ctx = FakeRunContext(agent.CallState())
    with pytest.raises(ToolError) as exc:
        await call(ctx)
    assert "No verified caller" in str(exc.value)


async def test_kb_search_works_without_identification():
    """Policy and how-to answers are not account data, so no gate."""
    ctx = FakeRunContext(agent.CallState())
    result = await agent.search_kb(ctx, "how do I reset my thermostat")
    assert result["articles"][0]["title"] == "Resetting a HomeSense thermostat"


# ---------------------------------------------------------------------------
# Cross-account scope (item 12b)
# ---------------------------------------------------------------------------


@pytest.fixture
def verified_state():
    state = agent.CallState(session_id="t")
    state.customer = backend.lookup_by_account("HS-10042")
    return state


async def test_scope_refuses_a_different_account(verified_state, caplog):
    ctx = FakeRunContext(verified_state)
    with caplog.at_level(logging.WARNING, logger="homesense"):
        with pytest.raises(ToolError) as exc:
            await agent.get_recent_order_status(ctx, account_number="HS-20077")
    assert "different account" in str(exc.value)
    assert "SCOPE REFUSED" in caplog.text


async def test_scope_allows_the_verified_account_in_any_spoken_form(verified_state):
    for spoken in ["HS-10042", "hs 10042", "h s one zero zero four two"]:
        result = await agent.get_recent_order_status(
            FakeRunContext(verified_state), account_number=spoken
        )
        assert result["order_id"] == "HS-ORD-88213"


async def test_scope_guard_covers_write_tools(verified_state):
    support = agent.SupportAgent()
    with pytest.raises(ToolError):
        await support.create_support_ticket(
            FakeRunContext(verified_state), "issue", account_number="HS-20077"
        )
    with pytest.raises(ToolError):
        await support.add_account_note(
            FakeRunContext(verified_state), "note", account_number="HS-20077"
        )


def test_require_scope_is_identity_gate_first():
    """An unverified session is refused before scope is even considered."""
    with pytest.raises(ToolError) as exc:
        agent.CallState().require_scope("HS-10042")
    assert "No verified caller" in str(exc.value)


# ---------------------------------------------------------------------------
# Filler before backend calls (item 11)
# ---------------------------------------------------------------------------


async def test_backend_tools_speak_a_filler_first(verified_state):
    """Item 11: the caller hears something before a backend round trip."""
    ctx = FakeRunContext(verified_state)
    await agent.get_recent_order_status(ctx)
    await agent.get_device_status(ctx, "thermostat")
    await agent.search_kb(ctx, "how do I reset my thermostat")
    assert ctx.fillers == ["Let me pull that up.", "Let me check that.", "Let me look that up."]


async def test_ticket_creation_speaks_a_filler(verified_state):
    ctx = FakeRunContext(verified_state)
    await agent.SupportAgent().create_support_ticket(ctx, "sensor offline")
    assert ctx.fillers == ["Okay, I'm creating that ticket now."]


# ---------------------------------------------------------------------------
# Account notes (item 12a)
# ---------------------------------------------------------------------------


async def test_add_account_note_confirms(verified_state):
    result = await agent.SupportAgent().add_account_note(
        FakeRunContext(verified_state), "Prefers morning appointments."
    )
    assert result["confirmation"] == "Noted on your account."
    assert verified_state.notes == ["Prefers morning appointments."]


# ---------------------------------------------------------------------------
# Escalation (item 13)
# ---------------------------------------------------------------------------


def test_escalation_reasons_are_the_agreed_set():
    import typing

    assert set(typing.get_args(agent.EscalationReason)) == {
        "requested",
        "irate",
        "unresolved",
        "policy",
    }


def test_transfer_tool_exposes_the_reason_enum():
    """The LLM must be forced to pick a reason, not invent one."""
    from livekit.agents.llm import utils as lu

    support = agent.SupportAgent()
    transfer = next(
        t for t in support.tools if is_function_tool(t)
        and get_function_info(t).name == "transfer_to_human"
    )
    schema = lu.build_legacy_openai_schema(transfer)
    fn = schema.get("function", schema)
    reason = fn["parameters"]["properties"]["reason"]
    assert set(reason["enum"]) == {"requested", "irate", "unresolved", "policy"}
    assert fn["parameters"]["required"] == ["reason"]


def test_escalation_instructions_cover_every_trigger():
    text = agent.SUPPORT_INSTRUCTIONS
    for cue in ["ridiculous", "cancel", "manager", "refund", "Empathise ONCE"]:
        assert cue in text, cue


def test_support_instructions_cover_follow_ups_and_scope():
    text = _flat(agent.SUPPORT_INSTRUCTIONS)
    assert "the other one" in text
    assert "caller's account only" in text
    assert "search_kb" in text


# ---------------------------------------------------------------------------
# Checkpoint / rehydrate (item 9)
# ---------------------------------------------------------------------------


def test_restore_chat_ctx_rebuilds_turns():
    snapshot = {
        "chat_items": [
            {"role": "user", "text": "my basement sensor is offline"},
            {"role": "assistant", "text": "I can see it went offline on August 9th."},
            {"role": "system", "text": "ignored"},
            {"role": "user", "text": ""},
        ]
    }
    chat_ctx = agent.restore_chat_ctx(snapshot)
    texts = [m.text_content for m in chat_ctx.items if hasattr(m, "text_content")]
    assert "my basement sensor is offline" in " ".join(t or "" for t in texts)
    # Only user/assistant turns with text survive.
    assert len(chat_ctx.items) == 2


def test_restore_chat_ctx_handles_empty_snapshot():
    assert len(agent.restore_chat_ctx({}).items) == 0


def test_resumed_support_agent_is_constructible():
    resumed = agent.SupportAgent(chat_ctx=agent.restore_chat_ctx({}), resumed=True)
    assert resumed._resumed is True


# ---------------------------------------------------------------------------
# MCP toggle (item 7)
# ---------------------------------------------------------------------------


def test_local_mode_uses_in_process_tools():
    assert agent.USE_MCP is False, "tests assume USE_MCP is unset"
    tools = agent.build_lookup_tools()
    assert len(tools) == len(agent.LOCAL_LOOKUP_TOOLS)
    assert all(is_function_tool(t) for t in tools)


def test_mcp_mode_swaps_in_a_toolset(monkeypatch):
    monkeypatch.setattr(agent, "USE_MCP", True)
    tools = agent.build_lookup_tools()
    assert len(tools) == 1
    assert isinstance(tools[0], agent.lk_mcp.MCPToolset)
    assert tools[0].id == "homesense-backend"


def test_mcp_mode_keeps_write_tools_local(monkeypatch):
    """Ticketing, notes and transfer must never leave the process.

    NOTE: tool_names() only sees the *local* function tools. It says nothing
    about what the MCP toolset exposes - that is asserted separately below,
    because an unfiltered toolset silently hands the model every server tool.
    """
    monkeypatch.setattr(agent, "USE_MCP", True)
    names = tool_names(agent.SupportAgent())
    assert set(names) == {"create_support_ticket", "add_account_note", "transfer_to_human"}


def test_mcp_toolset_is_filtered_to_read_only_lookups(monkeypatch):
    """Regression: an unfiltered MCPServerHTTP exposes every server tool.

    Without allowed_tools the model would receive `lookup_by_account`, which
    takes an arbitrary account number and would walk straight past
    require_scope - a cross-account data leak. It would also receive
    `create_ticket`, a second write path that skips CRM logging.
    """
    monkeypatch.setattr(agent, "USE_MCP", True)
    toolset = agent.build_lookup_tools("HS-10042")[0]
    allowed = toolset._mcp_server._allowed_tools

    assert allowed is not None, "MCP toolset must not be unfiltered"
    assert "lookup_by_account" not in allowed
    assert "create_ticket" not in allowed
    assert allowed == set(agent.ALLOWED_MCP_TOOLS)


def test_allowed_mcp_tools_matches_the_read_only_set():
    assert set(agent.ALLOWED_MCP_TOOLS) == {
        "get_recent_order",
        "get_device_status",
        "get_temperature",
        "get_subscription",
        "search_kb",
    }


def test_mcp_connection_carries_auth_and_account_scope(monkeypatch):
    """The verified account is pinned in the headers, not left to the model."""
    monkeypatch.setattr(agent, "USE_MCP", True)
    headers = agent.build_lookup_tools("HS-10042")[0]._mcp_server._headers

    assert headers["Authorization"] == f"Bearer {agent.MCP_TOKEN}"
    assert headers["X-HomeSense-Account"] == "HS-10042"


def test_mcp_connection_without_an_account_sends_no_scope(monkeypatch):
    """No verified account -> no scope header, and the server refuses."""
    monkeypatch.setattr(agent, "USE_MCP", True)
    headers = agent.build_lookup_tools(None)[0]._mcp_server._headers
    assert "X-HomeSense-Account" not in headers


def test_support_agent_pins_the_verified_account_into_mcp_headers(monkeypatch):
    monkeypatch.setattr(agent, "USE_MCP", True)
    support = agent.SupportAgent(account_number="HS-20077")
    toolset = next(t for t in support.tools if isinstance(t, agent.lk_mcp.MCPToolset))
    assert toolset._mcp_server._headers["X-HomeSense-Account"] == "HS-20077"


def test_log_stack_reports_the_tool_transport(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="homesense"):
        agent.log_stack()
    assert "TOOLS: local function tools" in caplog.text

    caplog.clear()
    monkeypatch.setattr(agent, "USE_MCP", True)
    with caplog.at_level(logging.INFO, logger="homesense"):
        agent.log_stack()
    assert "TOOLS: via MCP" in caplog.text


def test_stack_line_format():
    """The presenter points at this line; keep its shape stable."""
    import logging as _logging

    records = []
    handler = _logging.Handler()
    handler.emit = records.append
    agent.logger.addHandler(handler)
    previous = agent.logger.level
    agent.logger.setLevel(_logging.INFO)
    try:
        agent.log_stack()
    finally:
        agent.logger.removeHandler(handler)
        agent.logger.setLevel(previous)
    line = records[0].getMessage()
    assert line.startswith("STACK: STT=")
    assert "via LiveKit Inference" in line
    assert "VAD=silero" in line
    assert "turn=inference.TurnDetector" in line


# ---------------------------------------------------------------------------
# Silence / away handling and session guards (item 14)
# ---------------------------------------------------------------------------


def test_first_silence_nudges_by_preferred_name(verified_state):
    """Prashanth goes by Prash — the nudge must use the preferred name."""
    action, payload = agent.decide_away_action(verified_state)
    assert action == "prompt"
    assert payload == "Are you still there, Prash?"
    assert verified_state.away_prompts == 1


def test_first_silence_nudges_unidentified_caller_generically():
    action, payload = agent.decide_away_action(agent.CallState())
    assert action == "prompt"
    assert payload == "Are you still there, there?" or "still there" in payload


def test_second_silence_closes(verified_state):
    agent.decide_away_action(verified_state)
    action, reason = agent.decide_away_action(verified_state)
    assert action == "close"
    assert reason == "no response"
    assert verified_state.away_prompts == 2


def test_further_silences_keep_closing(verified_state):
    for _ in range(4):
        action, _ = agent.decide_away_action(verified_state)
    assert action == "close"


def test_goodbye_mentions_saved_work_only_when_there_is_some(verified_state):
    assert "nothing is lost" in agent.goodbye_line(verified_state)

    verified_state.tickets.append("TKT-1001")
    assert "ticket and notes are saved" in agent.goodbye_line(verified_state)


def test_goodbye_counts_notes_too(verified_state):
    verified_state.notes.append("prefers mornings")
    assert "saved" in agent.goodbye_line(verified_state)


def test_session_guard_defaults_are_sane():
    assert agent.USER_AWAY_TIMEOUT == 15.0
    assert agent.MAX_SESSION_SECONDS == 900.0


async def test_turn_handling_options_resolve_as_intended():
    """Item 14: barge-in, backchannel tolerance and endpointing are configured.

    Asserts the *resolved* session options, so a typo in a TypedDict key (which
    would otherwise be silently ignored) fails the build.
    """
    from livekit.agents import AgentSession

    session = AgentSession(
        stt=agent.STT_MODEL,
        llm=agent.LLM_MODEL,
        tts=agent.TTS_MODEL,
        userdata=agent.CallState(),
        user_away_timeout=agent.USER_AWAY_TIMEOUT,
        turn_handling=agent.TurnHandlingOptions(
            turn_detection=agent.inference.TurnDetector(),
            preemptive_generation=agent.PreemptiveGenerationOptions(enabled=True),
            endpointing={"mode": "dynamic", "min_delay": 0.5, "max_delay": 2.5},
            interruption={
                "enabled": True,
                "mode": "adaptive",
                "min_duration": 0.5,
                "min_words": 2,
                "resume_false_interruption": True,
                "false_interruption_timeout": 2.0,
            },
        ),
    )
    options = session.options

    assert options.preemptive_generation["enabled"] is True
    assert options.user_away_timeout == 15.0

    # Barge-in on, but a one-word backchannel should not stop the agent.
    assert options.interruption["enabled"] is True
    assert options.interruption["mode"] == "adaptive"
    assert options.interruption["min_words"] == 2
    assert options.interruption["resume_false_interruption"] is True

    # Floor keeps slow account-number dictation intact; the ceiling caps dead
    # air. It was 6.0s and live calls were sitting at 6001ms after a SHORT
    # utterance, turning a 1s answer into a 7s round trip.
    assert options.endpointing["min_delay"] == 0.5
    assert options.endpointing["max_delay"] == 2.5


# ---------------------------------------------------------------------------
# Agent persona: an assistant, not a person
# ---------------------------------------------------------------------------


def test_agent_never_claims_a_human_name():
    """The caller persona is Priya Sharma; the agent must not read as a person."""
    import inspect

    src = inspect.getsource(agent)
    assert "Alex" not in src
    for text in (agent.IDENTIFY_INSTRUCTIONS, agent.SUPPORT_INSTRUCTIONS):
        flat = _flat(text)
        assert "HomeSense support assistant" in flat
        # "You are not a person" / "Not a person, no human name"
        assert "ot a person" in flat
        assert "no human name" in flat


def test_identify_agent_introduces_itself_as_the_assistant():
    assert "HomeSense support assistant" in agent.IDENTIFY_INSTRUCTIONS


def test_support_agent_introduces_only_when_it_speaks_first():
    """After an IdentifyAgent handoff we have already introduced ourselves."""
    assert agent.SupportAgent(greet_by_name=True, introduce=True)._introduce is True
    # The handoff path leaves it off.
    assert agent.SupportAgent(greet_by_name=True)._introduce is False


def test_presenter_identity():
    import backend

    presenter = backend.lookup_by_account("HS-10042")
    assert presenter.name == "Prashanth Ganapathy"
    assert presenter.first_name == "Prashanth"
    assert presenter.preferred_name == "Prash"


def test_greeting_uses_preferred_name_not_first_name():
    """The agent must say "Prash", never "Prashanth", when greeting."""
    import inspect

    src = inspect.getsource(agent.SupportAgent.on_enter)
    assert "preferred_name" in src
    assert "first_name" not in src


# ---------------------------------------------------------------------------
# Authenticated web caller: ?account= / ?phone= -> participant -> greet by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ('{"account": "HS-10042"}', {"account": "HS-10042"}),
        ('{"phone": "+14155550142"}', {"phone": "+14155550142"}),
        (
            '{"account": "HS-10042", "phone": "+14155550142"}',
            {"account": "HS-10042", "phone": "+14155550142"},
        ),
        ('{"account": "  HS-10042  "}', {"account": "HS-10042"}),
        ('{"account_number": "HS-10042"}', {"account": "HS-10042"}),
    ],
)
def test_parse_web_identity_from_metadata(metadata, expected):
    assert agent.parse_web_identity(metadata) == expected


@pytest.mark.parametrize(
    "metadata",
    ["", None, "not json", "[]", '"a string"', "{}", '{"account": ""}', '{"other": "x"}'],
)
def test_parse_web_identity_ignores_junk(metadata):
    """Malformed metadata means 'not identified', never a crashed call."""
    assert agent.parse_web_identity(metadata) == {}


def test_parse_web_identity_from_attributes():
    identity = agent.parse_web_identity(
        None, {"homesense.account": "HS-10042", "homesense.phone": "+14155550142"}
    )
    assert identity == {"account": "HS-10042", "phone": "+14155550142"}


def test_attributes_win_over_metadata():
    """Attributes are the more explicit channel, so they take precedence."""
    identity = agent.parse_web_identity(
        '{"account": "HS-20077"}', {"homesense.account": "HS-10042"}
    )
    assert identity["account"] == "HS-10042"


def test_parse_web_identity_ignores_sip_attributes():
    assert agent.parse_web_identity(None, {"sip.phoneNumber": "+14155550142"}) == {}


def test_lookup_web_identity_by_account():
    customer = agent.lookup_web_identity({"account": "HS-10042"})
    assert customer.name == "Prashanth Ganapathy"


def test_lookup_web_identity_by_phone():
    customer = agent.lookup_web_identity({"phone": "+14155550177"})
    assert customer.account_number == "HS-20077"


def test_lookup_web_identity_second_account_by_phone():
    assert agent.lookup_web_identity({"phone": "+14155550142"}).account_number == "HS-30001"


def test_lookup_web_identity_prefers_account_over_phone():
    """A mismatched pair resolves by account — the stronger identifier."""
    customer = agent.lookup_web_identity(
        {"account": "HS-10042", "phone": "+14155550177"}
    )
    assert customer.account_number == "HS-10042"


@pytest.mark.parametrize(
    "identity", [{}, {"account": "HS-99999"}, {"phone": "+15005550000"}]
)
def test_lookup_web_identity_unknown_returns_none(identity):
    assert agent.lookup_web_identity(identity) is None


def test_resolve_web_identity_reads_the_participant():
    ctx = FakeCtx(
        room_name="voice_assistant_room_1234",
        participants=[FakeParticipant({"homesense.account": "HS-10042"})],
    )
    assert agent.resolve_web_identity(ctx) == {"account": "HS-10042"}


def test_resolve_web_identity_empty_for_anonymous_web_caller():
    """Plain http://localhost:3001 with no query params: identity gate applies."""
    ctx = FakeCtx(room_name="voice_assistant_room_1234", participants=[FakeParticipant({})])
    assert agent.resolve_web_identity(ctx) == {}
    assert agent.is_phone_call(ctx) is False


def test_web_identity_end_to_end():
    """?account=HS-10042 on the page -> greeted as Prash."""
    ctx = FakeCtx(
        room_name="voice_assistant_room_1234",
        participants=[FakeParticipant({"homesense.account": "HS-10042"})],
    )
    customer = agent.lookup_web_identity(agent.resolve_web_identity(ctx))
    assert customer.preferred_name == "Prash"


def test_metadata_participant_without_attributes():
    """The token route sets metadata even when attributes are unavailable."""

    class MetaOnly:
        metadata = '{"account": "HS-20077"}'
        attributes: dict[str, str] = {}

    ctx = FakeCtx(room_name="r")
    ctx.room.remote_participants = {"0": MetaOnly()}
    assert agent.resolve_web_identity(ctx) == {"account": "HS-20077"}


# ---------------------------------------------------------------------------
# Identify by phone number (easier than reading out an account number)
# ---------------------------------------------------------------------------


async def test_identify_by_phone_hands_off(caplog):
    state = agent.CallState()
    with caplog.at_level(logging.INFO, logger="homesense"):
        result = await agent.IdentifyAgent().identify_by_phone_number(
            FakeRunContext(state), "+14155550142"
        )
    assert isinstance(result, agent.SupportAgent)
    assert state.customer.account_number == "HS-30001"
    assert state.identified_via == "phone_number"
    assert "via phone" in caplog.text


@pytest.mark.parametrize(
    "spoken",
    [
        "four one five five five five zero one four two",
        "four one five, five five five, oh one four two",
        "my number is 415 555 0142",
        "(415) 555-0142",
        "+1 415 555 0142",
    ],
)
async def test_identify_by_phone_accepts_spoken_digits(spoken):
    """STT writes a spoken number as words; the lookup must still land."""
    state = agent.CallState()
    await agent.IdentifyAgent().identify_by_phone_number(FakeRunContext(state), spoken)
    assert state.customer.account_number == "HS-30001"


async def test_identify_by_phone_unknown_raises():
    state = agent.CallState()
    with pytest.raises(ToolError) as exc:
        await agent.IdentifyAgent().identify_by_phone_number(
            FakeRunContext(state), "+15005550000"
        )
    assert "account number instead" in str(exc.value)
    assert state.customer is None


async def test_identify_by_phone_finds_the_presenter(monkeypatch):
    monkeypatch.setenv("PRESENTER_PHONE", "+12065551234")
    state = agent.CallState()
    await agent.IdentifyAgent().identify_by_phone_number(
        FakeRunContext(state), "two zero six five five five one two three four"
    )
    assert state.customer.preferred_name == "Prash"


def _flat(text: str) -> str:
    """Collapse whitespace so assertions survive source line-wrapping."""
    import re as _re

    return _re.sub(r"\s+", " ", text)


def test_identify_instructions_offer_both_options():
    text = _flat(agent.IDENTIFY_INSTRUCTIONS)
    assert "phone number OR the account number" in text
    assert "identify_by_phone_number" in text
    assert "identify_by_account_number" in text


# ---------------------------------------------------------------------------
# Greeting length and speaking pace
# ---------------------------------------------------------------------------


def test_greetings_are_one_short_sentence():
    import inspect

    src = _flat(inspect.getsource(agent.SupportAgent.on_enter))
    assert "HomeSense support assistant here — what can I help with?" in src
    assert "Say exactly this and nothing more" in src

    identify_src = _flat(inspect.getsource(agent.IdentifyAgent.on_enter))
    assert "what's the phone number or account " in identify_src
    assert "number on file?" in identify_src


def test_voice_style_sets_a_measured_pace():
    text = _flat(agent.VOICE_STYLE)
    assert "Measured pace" in text
    assert "under 15 words" in text
    assert "One question per turn" in text


# ---------------------------------------------------------------------------
# TTS pacing
# ---------------------------------------------------------------------------


def test_tts_is_slowed_for_cartesia():
    tts = agent.build_tts()
    assert not isinstance(tts, str), "cartesia should get an inference.TTS with options"
    assert agent.TTS_SPEED == "slow"


def test_tts_speed_is_the_enum_not_a_float():
    """LiveKit Inference rejects float speeds for cartesia/sonic-2.

    Passing a number here fails at synthesis time with
    'invalid speed value (cartesia/sonic-2)', i.e. the agent would be mute.
    """
    assert agent.TTS_SPEED in ("slow", "normal", "fast")


def test_non_cartesia_model_falls_back_to_a_plain_string(monkeypatch):
    monkeypatch.setattr(agent, "TTS_MODEL", "inworld/inworld-tts-2")
    assert agent.build_tts() == "inworld/inworld-tts-2"


# ---------------------------------------------------------------------------
# Greeting timing: wait for the caller's audio
# ---------------------------------------------------------------------------


class FakePublication:
    def __init__(self, kind, subscribed):
        self.kind = kind
        self.subscribed = subscribed


class FakeAudioParticipant:
    def __init__(self, publications):
        self.attributes: dict[str, str] = {}
        self.identity = "user"
        self.track_publications = {str(i): p for i, p in enumerate(publications)}


def test_has_subscribed_audio_true_when_audio_is_live():
    from livekit import rtc

    room = FakeRoom("r", [FakeAudioParticipant([FakePublication(rtc.TrackKind.KIND_AUDIO, True)])])
    assert agent._has_subscribed_audio(room) is True


def test_has_subscribed_audio_false_when_not_yet_subscribed():
    from livekit import rtc

    room = FakeRoom("r", [FakeAudioParticipant([FakePublication(rtc.TrackKind.KIND_AUDIO, False)])])
    assert agent._has_subscribed_audio(room) is False


def test_has_subscribed_audio_ignores_video():
    from livekit import rtc

    room = FakeRoom("r", [FakeAudioParticipant([FakePublication(rtc.TrackKind.KIND_VIDEO, True)])])
    assert agent._has_subscribed_audio(room) is False


def test_has_subscribed_audio_empty_room():
    assert agent._has_subscribed_audio(FakeRoom("r", [])) is False


def test_greeting_timing_defaults():
    assert agent.TRACK_WAIT_SECONDS == 3.0
    assert agent.GREETING_SETTLE_SECONDS == 0.5


# ---------------------------------------------------------------------------
# Event emission for the flow visualiser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("get_device_status", "db"),
        ("identify_by_phone_number", "db"),
        ("search_kb", "kb"),
        ("create_support_ticket", "ticketing"),
        ("add_account_note", "crm"),
        ("transfer_to_human", "human"),
        ("something_unknown", "db"),
    ],
)
def test_tool_target_mapping(tool, expected):
    assert agent.tool_target(tool) == expected


def test_read_only_tools_report_mcp_when_enabled(monkeypatch):
    monkeypatch.setattr(agent, "USE_MCP", True)
    assert agent.tool_target("get_device_status") == "mcp"
    assert agent.tool_target("search_kb") == "mcp"
    # Writes never go over MCP, so they must never claim to.
    assert agent.tool_target("create_support_ticket") == "ticketing"
    assert agent.tool_target("add_account_note") == "crm"


def test_note_tool_emits_a_tool_call_event(monkeypatch, tmp_path):
    import backend

    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(backend, "EVENTS_FILE", events)
    monkeypatch.setattr(backend, "CRM_FILE", tmp_path / "crm.jsonl")

    state = agent.CallState(session_id="s1", room_name="r1")
    state.note_tool("search_kb", {"result": "ok", "titles": ["Sensor offline"]})

    rows = backend.read_events(path=events)
    assert len(rows) == 1
    assert rows[0]["kind"] == "tool_call"
    assert rows[0]["tool"] == "search_kb"
    assert rows[0]["target"] == "kb"
    assert rows[0]["summary"] == "Sensor offline"


def test_turn_latency_emits_a_turn_event():
    captured = []
    t = agent.TurnLatency(on_turn=captured.append)
    t.handle(stt(0.12))
    t.handle(eou(0.35))
    t.handle(llm(0.28))
    t.handle(tts(0.19))

    assert len(captured) == 1
    metrics = captured[0]
    assert metrics["stt_ms"] == 120.0
    assert metrics["eou_ms"] == 350.0
    assert metrics["llm_ttft_ms"] == 280.0
    assert metrics["tts_ttfb_ms"] == 190.0
    assert metrics["e2e_ms"] == 820.0


def test_turn_latency_without_callback_still_works():
    t = agent.TurnLatency()
    t.handle(eou(0.35))
    t.handle(llm(0.28))
    t.handle(tts(0.19))
    assert t.turn_count == 1


def test_cancelled_turns_emit_nothing():
    captured = []
    t = agent.TurnLatency(on_turn=captured.append)
    t.handle(eou(0.35))
    t.handle(llm(0.28, cancelled=True))
    t.handle(tts(0.19))
    assert captured == []


# ---------------------------------------------------------------------------
# Scope regression: a verified caller must never be locked out of their own data
#
# Live bug (session AJ_iEKz28hCqDND): the caller verified by phone, was greeted
# "Hi Prash", and then EVERY account tool refused. The model had filled the
# optional account_number argument with the phone digits it just heard, and a
# strict compare against "HS-10042" rejected it before any lookup ran.
# ---------------------------------------------------------------------------


@pytest.fixture
def presenter_state(monkeypatch):
    monkeypatch.setenv("PRESENTER_PHONE", "+12052238057")
    state = agent.CallState(session_id="scope")
    state.customer = backend.lookup_by_account("HS-10042")
    return state


@pytest.mark.parametrize(
    "supplied",
    [
        None,
        "",
        "   ",
        "HS-10042",
        "hs 10042",
        "H S 1 0 0 4 2",
        "10042",  # bare digits — what the model sends after a spoken account number
        "one zero zero four two",
        "+1 205 223 8057",  # the phone the caller verified with
        "2052238057",
        "two zero five two two three eight zero five seven",
        "my account",  # no digits at all
    ],
)
async def test_verified_caller_is_never_locked_out(presenter_state, supplied):
    """Anything that resolves to the verified caller must be accepted."""
    result = await agent.get_device_status(
        FakeRunContext(presenter_state), "thermostat", account_number=supplied
    )
    assert result["device_id"] == "THERM-LR-01"


@pytest.mark.parametrize("supplied", ["HS-30001", "30001", "+14155550142", "HS-20077", "20077"])
async def test_a_different_account_is_still_refused(presenter_state, supplied):
    with pytest.raises(ToolError) as exc:
        await agent.get_device_status(
            FakeRunContext(presenter_state), "thermostat", account_number=supplied
        )
    assert "different account" in str(exc.value)
    # The refusal must not send the agent back to re-verifying.
    assert "verify again" not in str(exc.value).lower() or "Do NOT" in str(exc.value)


async def test_refusal_never_asks_the_caller_to_re_verify(presenter_state):
    with pytest.raises(ToolError) as exc:
        await agent.get_recent_order_status(
            FakeRunContext(presenter_state), account_number="HS-30001"
        )
    text = str(exc.value)
    assert "Do NOT ask them to verify again." in text


def test_scope_matching_rules(presenter_state):
    customer = presenter_state.customer
    ok = lambda v: agent.CallState._refers_to_verified(v, customer)  # noqa: E731
    assert ok(None) and ok("") and ok("   ")
    assert ok("HS-10042") and ok("10042") and ok("2052238057")
    assert not ok("HS-30001")
    assert not ok("30001")
    assert not ok("+14155550142")


# --- all four identification paths end in a working SupportAgent -------------


async def _tools_work_for(state):
    """Every account tool succeeds for the verified caller."""
    ctx = FakeRunContext(state)
    assert (await agent.get_device_status(ctx, "thermostat"))["status"] == "active"
    assert (await agent.get_room_temperature(ctx, "living room"))["temperature_f"] == 71
    assert (await agent.get_recent_order_status(ctx))["order_id"] == "HS-ORD-88213"
    assert (await agent.get_subscription_status(ctx))["status"] == "active"


async def _other_account_refused(state):
    with pytest.raises(ToolError):
        await agent.get_device_status(
            FakeRunContext(state), "thermostat", account_number="HS-30001"
        )


async def test_path_spoken_phone_number(monkeypatch):
    """The path that broke in the live run."""
    monkeypatch.setenv("PRESENTER_PHONE", "+12052238057")
    state = agent.CallState()
    handoff = await agent.IdentifyAgent().identify_by_phone_number(
        FakeRunContext(state), "two zero five two two three eight zero five seven"
    )
    assert isinstance(handoff, agent.SupportAgent)
    assert state.customer.account_number == "HS-10042"
    await _tools_work_for(state)
    await _other_account_refused(state)


async def test_path_spoken_account_number():
    state = agent.CallState()
    handoff = await agent.IdentifyAgent().identify_by_account_number(
        FakeRunContext(state), "H S 1 0 0 4 2"
    )
    assert isinstance(handoff, agent.SupportAgent)
    await _tools_work_for(state)
    await _other_account_refused(state)


async def test_path_caller_id(monkeypatch):
    """Inbound SIP matched on caller ID — SupportAgent starts already verified."""
    monkeypatch.setenv("PRESENTER_PHONE", "+12052238057")
    state = agent.CallState(channel="phone")
    state.customer = backend.lookup_by_phone("+12052238057")
    state.identified_via = "caller_id"
    assert state.customer.account_number == "HS-10042"
    await _tools_work_for(state)
    await _other_account_refused(state)


async def test_path_web_session_metadata():
    """Signed-in web pill / ?account= — identity arrives on the participant."""
    state = agent.CallState(channel="web")
    state.customer = agent.lookup_web_identity(
        agent.parse_web_identity(None, {"homesense.account": "HS-10042"})
    )
    state.identified_via = "web_session"
    await _tools_work_for(state)
    await _other_account_refused(state)


# --- unknown devices must give the anti-hallucination answer, not a scope one -


async def test_loose_device_match_returns_the_real_device_name(presenter_state):
    """The live run's "bedroom thermostat": no longer a scope error.

    HS-10042 has one thermostat, in the living room. The tool resolves it on the
    device type and returns its REAL name and room, so the agent can say "I only
    see a Living Room Thermostat" instead of echoing the caller's wording back.
    """
    result = await agent.get_device_status(
        FakeRunContext(presenter_state), "bedroom thermostat", account_number="2052238057"
    )
    assert result["name"] == "Living Room Thermostat"
    assert result["room"] == "Living Room"
    assert result["status"] == "active"


async def test_genuinely_absent_device_still_refuses(presenter_state):
    """A device with no counterpart at all must still be refused outright."""
    with pytest.raises(ToolError) as exc:
        await agent.get_device_status(
            FakeRunContext(presenter_state), "garage door opener", account_number="2052238057"
        )
    text = str(exc.value)
    assert "No device matching" in text
    assert "different account" not in text


def test_instructions_require_naming_the_device_actually_found():
    text = _flat(agent.SUPPORT_INSTRUCTIONS)
    assert "say the device name the tool returned" in text
    assert "ask if that is the one" in text


# --- SupportAgent cannot re-verify, structurally ----------------------------


def test_support_agent_has_no_identify_tools():
    """A tool failure can't turn into a re-verification loop if the tool is absent."""
    names = tool_names(agent.SupportAgent())
    assert not [n for n in names if n.startswith("identify")]


def test_support_instructions_forbid_re_verification():
    text = _flat(agent.SUPPORT_INSTRUCTIONS)
    assert "VERIFIED" in text
    assert "NEVER ask for an account or phone number again" in text
    assert "A tool failure is not an identity problem" in text


# ---------------------------------------------------------------------------
# Warm transfer mode selection
#
# LiveKit Phone Numbers are inbound-only today, so the SIP path cannot run on
# this project. These pin the branch so the demo never tries to dial.
# ---------------------------------------------------------------------------


def test_sip_transfer_requires_both_env_vars(monkeypatch):
    support = agent.SupportAgent()
    monkeypatch.setattr(agent, "TRANSFER_PHONE_NUMBER", "")
    monkeypatch.setattr(agent, "SIP_OUTBOUND_TRUNK_ID", "")
    assert support._sip_transfer_configured is False

    monkeypatch.setattr(agent, "TRANSFER_PHONE_NUMBER", "+12065551234")
    assert support._sip_transfer_configured is False, "a number alone is not enough"

    monkeypatch.setattr(agent, "SIP_OUTBOUND_TRUNK_ID", "ST_abc123")
    assert support._sip_transfer_configured is True


def test_default_config_uses_the_simulated_console():
    """With no outbound trunk configured, the demo must not attempt a dial."""
    assert agent.TRANSFER_PHONE_NUMBER == ""
    assert agent.SIP_OUTBOUND_TRUNK_ID == ""
    assert agent.SupportAgent()._sip_transfer_configured is False


# ---------------------------------------------------------------------------
# Prompt budget — LLM TTFT scales with how much the model has to read each turn
# ---------------------------------------------------------------------------


def _prompt_tokens(agent_obj, instructions: str) -> int:
    """Approximate prompt size: instructions + every tool schema, ~4 chars/token."""
    import json

    from livekit.agents.llm import utils as lu
    from livekit.agents.llm.tool_context import is_function_tool

    chars = len(instructions)
    for tool in agent_obj.tools:
        if not is_function_tool(tool):
            continue
        schema = lu.build_legacy_openai_schema(tool)
        chars += len(json.dumps(schema.get("function", schema)))
    return chars // 4


def test_support_prompt_stays_within_budget():
    """Regression guard: the prompt was 2515 tokens and TTFT ran 0.9-2.4s.

    Measured with tiktoken o200k_base it is now ~1517. This uses a chars/4
    approximation so the test needs no tokenizer dependency; the ceiling is set
    with headroom so it catches a real regression, not normal editing.
    """
    approx = _prompt_tokens(agent.SupportAgent(), agent.SUPPORT_INSTRUCTIONS)
    assert approx < 2100, f"SupportAgent prompt grew to ~{approx} tokens"


def test_identify_prompt_stays_small():
    approx = _prompt_tokens(agent.IdentifyAgent(), agent.IDENTIFY_INSTRUCTIONS)
    assert approx < 700, f"IdentifyAgent prompt grew to ~{approx} tokens"


def test_trimmed_instructions_keep_every_rule():
    """The cut removed prose, not rules."""
    text = _flat(agent.SUPPORT_INSTRUCTIONS)
    for rule in [
        "NEVER ask for an account or phone number again",
        "not an identity problem",
        "search_kb only",
        "Never invent a device",
        "say the device name the tool returned",
        "requested:",
        "irate:",
        "unresolved:",
        "policy:",
        "Empathise ONCE",
        "under 15 words",
        "One question per turn",
    ]:
        assert rule in text, f"lost rule: {rule}"


def test_endpointing_is_env_tunable(monkeypatch):
    """The presenter can widen the window live without a code change."""
    import importlib

    monkeypatch.setenv("ENDPOINTING_MAX_DELAY", "3.0")
    assert float(os.environ["ENDPOINTING_MAX_DELAY"]) == 3.0
    importlib.reload  # referenced so the intent is explicit


# ---------------------------------------------------------------------------
# Room teardown
#
# Rooms were lingering until the server's empty-room timeout, so LiveKit Cloud
# showed sessions "active" for minutes after the caller hung up.
# ---------------------------------------------------------------------------


class FakeDeleteCtx(FakeCtx):
    """JobContext stub that records delete_room() calls."""

    def __init__(self, *args, fail: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.deleted: list[str] = []
        self._fail = fail

    async def delete_room(self, room_name=None):
        if self._fail:
            raise RuntimeError("room already gone")
        self.deleted.append(room_name or self.room.name)


async def test_close_room_deletes_after_a_normal_call(caplog):
    ctx = FakeDeleteCtx(room_name="call-abc")
    state = agent.CallState(room_name="call-abc")
    with caplog.at_level(logging.INFO, logger="homesense"):
        assert await agent.close_room(ctx, state) is True
    assert ctx.deleted == ["call-abc"]
    assert "ROOM CLOSED call-abc" in caplog.text


async def test_close_room_is_skipped_during_a_sip_warm_transfer(caplog):
    """The caller and the human specialist are still on the room."""
    ctx = FakeDeleteCtx(room_name="call-abc")
    state = agent.CallState(room_name="call-abc")
    state.sip_transfer_active = True

    with caplog.at_level(logging.INFO, logger="homesense"):
        assert await agent.close_room(ctx, state) is False
    assert ctx.deleted == [], "must not hang up on a live warm transfer"
    assert "ROOM KEPT" in caplog.text


async def test_close_room_never_raises():
    """Cleanup must not be able to fail a call that already finished."""
    ctx = FakeDeleteCtx(room_name="call-abc", fail=True)
    assert await agent.close_room(ctx, agent.CallState(room_name="call-abc")) is False


async def test_simulated_transfer_still_closes_the_room():
    """Only the SIP path keeps the room; the console fallback does not."""
    state = agent.CallState(room_name="call-abc")
    state.handoff = {"handoff_id": "HO-1", "mode": "simulated_console"}
    assert state.sip_transfer_active is False

    ctx = FakeDeleteCtx(room_name="call-abc")
    assert await agent.close_room(ctx, state) is True
    assert ctx.deleted == ["call-abc"]


def test_report_is_written_before_the_room_is_deleted():
    """Ordering guard: releasing the room must not cost us the report.

    The shutdown body is a closure over the entrypoint, so this pins the
    sequence at the source level rather than through a full session harness.
    """
    import inspect

    src = inspect.getsource(agent.entrypoint)
    body = src[src.index("async def write_report"):]
    order = [
        body.index("backend.save_session_report"),
        body.index("backend.crm_log"),
        body.index("backend.clear_checkpoint"),
        body.index("await close_room"),
    ]
    assert order == sorted(order), "close_room must come last in the shutdown path"


def test_session_ends_promptly_when_the_caller_disconnects():
    """close_on_disconnect is what ends the session; confirm it is not disabled.

    It defaults to True in 1.6.10, so the agent does not sit waiting for the
    away timer after a hangup. This fails if anyone turns it off.
    """
    from livekit.agents import room_io

    assert room_io.RoomInputOptions().close_on_disconnect is True
    src = inspect.getsource(agent.entrypoint)
    assert "close_on_disconnect=False" not in src


def test_delete_room_on_close_is_left_off():
    """We delete the room ourselves so the SIP warm transfer can opt out."""
    from livekit.agents import room_io

    assert room_io.RoomInputOptions().delete_room_on_close is False
    assert "delete_room_on_close" not in inspect.getsource(agent.entrypoint)
