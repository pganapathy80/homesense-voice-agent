# HomeSense Voice Agent

A complete, working customer-service **voice agent** built on
[LiveKit Agents](https://docs.livekit.io/agents), with a Next.js web client and
live operator dashboards. HomeSense is a fictional smart-home company; the
agent answers inbound support calls over the phone (SIP) and the web (WebRTC).

What the agent does on a call:

1. **Identifies the caller** — by phone number on SIP calls, by spoken account
   number on web, behind a structural identity gate (the unverified agent has
   no account tools at all)
2. **Answers from account data** — orders, devices, room temperature,
   subscription — plus a small knowledge base for how-to and policy questions
3. **Creates tickets** when it can't resolve an issue, and reads the number back
4. **Logs everything to a CRM** — every tool call and transcript turn, plus an
   end-of-call report
5. **Warm-transfers to a human** — with an LLM-generated summary so the caller
   never repeats themselves

All models (STT, LLM, TTS, turn detection) run through **LiveKit Inference**,
so the only credentials needed are your LiveKit project keys.

## Repository layout

| Directory | What it is |
|---|---|
| [`demo/`](demo/) | The Python agent, mock backend, MCP server, tests, and the full run-book |
| [`webapp/`](webapp/) | Next.js web client (based on LiveKit's `agent-starter-react`) with three live operator screens: **Live Flow**, **Agent Desk**, and **Metrics** |

## Quick start

```bash
# 1. The agent
cd demo
uv sync
cp .env.example .env.local     # then add your LiveKit keys
uv run agent.py dev

# 2. The web client (second terminal)
cd webapp
pnpm install
cp .env.example .env.local     # same LiveKit keys, leave AGENT_NAME blank
pnpm dev                       # http://localhost:3001
```

Open http://localhost:3001 and click **"Call HomeSense Support"**. No keys are
needed to run the test suite: `cd demo && uv run pytest`.

The full walkthrough — demo script, example prompts, security model, latency
notes, observability, troubleshooting — is in [`demo/README.md`](demo/README.md).
The exact stack (models, components, versions) is in
[`demo/STACK.md`](demo/STACK.md).

## Highlights

- **Structural security, not prompt rules** — identity gating via agent
  handoff, per-session account scope on every tool, and an authenticated,
  scope-checked MCP boundary
- **Latency engineering** — preemptive generation, semantic turn detection,
  prompt-size budget with a test guarding it, per-turn latency logging
  (`e2e = eou + llm_ttft + tts_ttfb`)
- **Fault tolerance** — per-room JSON checkpoints with expiry; a crashed agent
  rehydrates the conversation on redispatch
- **No invented data** — every account fact goes through a tool; lookup logic
  is built for what STT actually returns (spoken digits, misheard letters)
- **Fully tested** — the mock backend has no LiveKit imports and a large test
  suite that runs with no network or credentials

## License

MIT — see [LICENSE](LICENSE). The `webapp/` directory is derived from
LiveKit's MIT-licensed
[`agent-starter-react`](https://github.com/livekit-examples/agent-starter-react)
template (see [`webapp/LICENSE`](webapp/LICENSE)).
