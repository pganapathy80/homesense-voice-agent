# The stack

Every layer of this demo runs on LiveKit. There is no second vendor in the
path — no Deepgram, OpenAI or Cartesia account, and no API key for any of them.
The only credentials in `.env.local` are `LIVEKIT_URL`, `LIVEKIT_API_KEY` and
`LIVEKIT_API_SECRET`.

## Layers

| Layer | Provider / model | LiveKit component |
|---|---|---|
| Transport & rooms | LiveKit Cloud (`wss://…livekit.cloud`) | WebRTC SFU, room lifecycle |
| Telephony | LiveKit SIP — an inbound LiveKit Phone Number, dispatch rule (Individual, room prefix `call-`) | SIP service → room |
| Dispatch | **Automatic** (no `agent_name`) | Agent worker registration |
| Orchestration | `livekit-agents` 1.6.10 `AgentSession` | Agent framework |
| Voice activity detection | **Silero VAD**, local, loaded in `prewarm` | `livekit.plugins.silero` |
| Turn detection | **LiveKit turn detector** (semantic end-of-turn) | `inference.TurnDetector()` |
| Speech to text | **`deepgram/nova-3`** | LiveKit Inference |
| Language model | **`openai/gpt-4.1-mini`** | LiveKit Inference |
| Text to speech | **`cartesia/sonic-2`** | LiveKit Inference |
| Latency optimisation | `preemptive_generation=True` | `TurnHandlingOptions` |
| Observability | Per-turn latency + usage in the agent log; full session detail in LiveKit Cloud → Sessions | `metrics_collected`, `UsageCollector` |
| Tools (write path) | In-process Python `@function_tool` — ticketing, notes, transfer | `livekit.agents.function_tool` |
| Tools (read path) | In-process by default; **MCP over streamable HTTP** with `USE_MCP=1`, restricted to 5 read-only tools and scoped to the verified account | `MCPServerHTTP(allowed_tools=…)` + `MCPToolset` |
| Knowledge base | `data/kb/*.md`, keyword ranking in pure Python | `search_kb` function tool |
| Warm transfer | Simulated console by default; SIP dial-in when a trunk is set | `ctx.add_sip_participant` |
| Crash resilience | JSON checkpoint per room, rehydrated on redispatch | `conversation_item_added` |
| Web client | `agent-starter-react` (Next.js) on `localhost:3001` | LiveKit JS SDK |

All of the above runs on **`livekit-agents` 1.6.10**. Note that this version
moved on from the 1.5.x shape: `AgentServer` replaces `WorkerOptions`, models
are `inference` strings, and per-kwarg turn settings are superseded by
`TurnHandlingOptions` (deprecated for removal in v2.0).

## Proving it live

The agent logs the whole stack on startup — point at these two lines:

```
STT=deepgram/nova-3  LLM=openai/gpt-4.1-mini  TTS=cartesia/sonic-2  via LiveKit Inference
VAD=silero (local, prewarmed)  turn-detector=livekit inference.TurnDetector  preemptive_generation=True  dispatch=automatic (no agent_name)
```

Then per turn:

```
TURN 1  stt=118ms  eou=352ms  llm_ttft=281ms  tts_ttfb=194ms  e2e=827ms  tokens=140
```

`e2e` is `eou + llm_ttft + tts_ttfb` — how long the line was actually quiet
before the agent started speaking. LiveKit Cloud → Sessions → **Models** tab
shows the same models server-side, which is the independent confirmation.

## Why LiveKit Inference here

- **One vendor, one bill, one set of credentials.** Swapping the LLM is a
  one-string change with no new account, key rotation or SDK.
- **Fewer network hops.** STT/LLM/TTS are resolved inside LiveKit's network
  rather than three round trips out to three providers.
- **Model portability.** `openai/gpt-4.1-mini` → `google/gemini-3-flash` is an
  edit to one string in `.env.local`, no code change.

Where a customer needs a specific provider account or a self-hosted model, the
plugin path (`livekit.plugins.openai`, `.deepgram`, `.cartesia`, …) drops into
exactly the same `AgentSession` slots — that's the migration story, and nothing
else in the agent changes.

## Changing a model

Set any of these in `.env.local` — no code change:

```
STT_MODEL=deepgram/nova-3
LLM_MODEL=openai/gpt-4.1-mini
TTS_MODEL=cartesia/sonic-2
```

Current model strings: <https://docs.livekit.io/agents/models/>
