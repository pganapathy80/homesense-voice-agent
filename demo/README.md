# HomeSense Voice Agent — demo

A working LiveKit voice agent. HomeSense is a fictional smart-home
company; the agent handles inbound support calls over phone and web.

It does five things:

1. **Identifies the caller** — by phone number on SIP calls, by spoken account number on web, behind a structural identity gate
2. **Answers from account data** — order status, device status, room temperature, subscription, registered devices — plus a knowledge base for how-to and policy questions
3. **Creates a ticket** when it can't resolve the issue, and reads back the ticket number
4. **Logs the interaction to the CRM** — every tool call and transcript turn, plus an end-of-call report
5. **Warm-transfers to a human** — with an LLM-generated summary so the human doesn't make the caller repeat themselves

Built on `livekit-agents 1.6.10`. All models come from **LiveKit Inference**, so
the only credentials needed are your LiveKit project keys — no OpenAI, Deepgram
or Cartesia account required.

---

## Setup

```bash
cd demo
uv sync
cp .env.example .env.local
```

Then put your LiveKit project credentials in `.env.local` (from
[cloud.livekit.io](https://cloud.livekit.io) → your project → Settings → Keys):

```
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
```

If you have the LiveKit CLI, this writes them for you:

```bash
lk app env -w .env.local
```

Optionally pre-fetch the Silero VAD weights (they ship with the package, so this
is usually a no-op — but it's free insurance against a cold start):

```bash
uv run -m livekit.agents download-files
```

> Note: `uv run agent.py download-files` also works but prints a deprecation
> warning as of 1.5.10. Use the `-m livekit.agents` form.

### Verify without credentials

```bash
uv run pytest          # 276 tests, no network or keys needed
```

---

## Running it

### The live demo — three terminals

```bash
# Terminal 1 — the agent
cd demo && uv run agent.py dev

# Terminal 2 — the HomeSense web app
cd webapp && pnpm dev          # http://localhost:3001

# Terminal 3 (optional) — terminal version of the human specialist screen
cd demo && uv run console_human.py

# Terminal 4 (optional) — the same lookups served over MCP
cd demo && uv run mcp_server.py            # http://localhost:8770/mcp
# ...then start the agent in terminal 1 with:  USE_MCP=1 uv run agent.py dev
```

Then open the browser tabs you need. There are three operator screens, each
answering a different question about the system:

| Tab | URL | What it is | What it answers |
|---|---|---|---|
| **1** | **http://localhost:3001** | The customer app — click **"Call HomeSense Support"** | the call itself |
| **2** | **http://localhost:3001/flow** | **Live Flow** — the architecture diagram lighting up as the call moves through it | "what's actually happening?" |
| **3** | **http://localhost:3001/desk** | **Agent Desk** — handoffs, tickets, CRM feed | the support-operations view |
| **4** | **http://localhost:3001/stats** | **Metrics** — latency, containment, cost | "does this pay for itself?" |

The three operator screens share a top nav, so you can move between them in one
click without hunting for URLs. The customer call page is untouched.

**Show these, not a terminal.** It is the same data as
`console_human.py` (it reads the same JSONL files), but it looks like software a
support team would actually use. Three live columns, polling every 2 seconds:
**Incoming handoffs** with the AI summary rendered field by field and an
**Accept** button, **Tickets**, and a **CRM activity** feed. New rows flash teal
as they land, so you watch tickets and handoffs appear *during* the call.

**Live Flow** (`/flow`) is the one to open while the call is running. It draws
the system architecture — Caller → LiveKit Cloud → Agent worker
(STT · LLM · TTS via LiveKit Inference) → Tools → Human Desk — and pulses each
node as the call actually reaches it, with a dot travelling the edge and the
real per-stage milliseconds on the STT/LLM/TTS boxes. A ticker at the bottom
narrates the last six events in plain words. It answers "is this real, or a
video?" without you having to claim anything.

**Metrics** (`/stats`) is the one to open at the end of a run: voice-to-voice
latency (avg and p95), containment vs transfer rate with the reason breakdown,
tickets, KB hit rate, average handle time, and an estimated cost per call
against a human baseline. Every number is computed from the same event stream,
live.

`console_human.py` still works and is the fallback if the browser misbehaves.

The web app has `AGENT_NAME` blank, which means **automatic dispatch** — it
creates a room and LiveKit hands the job to any registered worker. The agent
registers with no `agent_name` to match. Naming the agent would silently stop it
joining those rooms; there's a test guarding against that.

> `agent.py dev` prints a deprecation notice pointing at `lk agent dev` (the
> LiveKit CLI is taking over hot-reload). It still works and is the fewest
> moving parts for a demo — ignore the notice, or use `lk agent dev` if you
> prefer. Either way the worker registers with **`agent_name: ""`**, which is
> the line to look for:
>
> ```
> registered worker {"agent_name": "", "id": "AW_…", "region": "US West C"}
> ```

**Terminal-only sanity check** (no browser, no web app) — good for a quick
pre-flight:

```bash
uv run agent.py console
```

Add `--text-mode` to type instead of talk.

**Fallback client** if the Next.js app misbehaves: the **Agent Console** in the
LiveKit Cloud dashboard (Agents → **Launch Console**) talks to a locally running
agent just as well.

### Web (logged-in) beat

The app has a **"Signed in as Prash G. · HS-10042"** pill in the top right.
Leave it signed in and click **Call HomeSense Support** → the agent opens with
*"Hi Prash, HomeSense support assistant here — what can I help with?"*, no
verification asked for.

Click **Sign out** in that pill and call again → *"Hi, you've reached HomeSense
support. To pull up your account, what's the phone number or account number on
file?"* Give it either **"H S 1 0 0 4 2"** or the phone number on the account
and it verifies you, then continues as normal.

That contrast is the identity gate, and it is the thing to show back to back:
same agent, same code, and the only difference is whether the session arrived
with an identity attached.

How it works: the pill's state (localStorage) plus any `?account=` / `?phone=`
on the URL is sent to `/api/token`, which stamps `{"account":"HS-10042"}` onto
the participant token as metadata and `homesense.account` as an attribute. A URL
param always overrides the pill, so `?account=HS-30001` still works for a quick
persona switch. The agent waits for the participant, reads either one, and logs:

```
IDENTIFIED via web session metadata account=HS-10042 -> Prashanth Ganapathy
```

If the value matches no account the agent falls back to asking, so a bad URL or
a stale pill degrades gracefully rather than failing.

**Verifying by phone.** When signed out, the caller can say the phone number
instead of the account number — usually easier out loud. Spoken digits work:
*"four one five, five five five, oh one four two"* resolves fine, because the
same word-to-digit normalisation used for account numbers is applied to phone
numbers too.

> Be straight about this one: the identity comes from a query
> parameter, so it is a *claim*, not proof. It stands in for "the customer is
> already signed in to our app". In production the token route would read the
> session server-side and never trust a URL. The point of the beat is the agent
> side — that it accepts a pre-verified identity from the session and skips
> straight to helping.

### Phone beat

Set your mobile in `.env.local` first:

```
PRESENTER_PHONE=+1206XXXXXXX
```

Then **call your LiveKit phone number** from that phone. The agent greets you
by name — same agent, same code path, same room model, just entered over SIP
instead of WebRTC. Nothing is dispatched differently; the SIP service simply
puts the caller into a room named `call-…` and automatic dispatch does the rest.

Your account is **HS-10042**, which is the *same dataset as the main demo
script* — so the whole five-beat script works identically over the phone. Without
`PRESENTER_PHONE` set, that account falls back to an unroutable
`+10000000000` and can never match a real caller.

---

## The 5-beat demo script

Account to use: **HS-10042** — **Prashanth "Prash" Ganapathy**, reachable on
whatever you set as `PRESENTER_PHONE`. That is *you*, so the demo reads as your
own support call.

On `console` / plain web you are *not* identified, so beat 1 is the account
number handoff. Calling from your `PRESENTER_PHONE` cell — or opening
`?account=HS-10042` — greets you as Prash and skips straight to beat 2.

| # | You say | What happens |
|---|---------|---------------------|
| 1 | *"Hi, I need help with my account."* → then when asked, either **"H S 1 0 0 4 2"** or the phone number on the account | Agent verifies and greets you as **Prash**. Identity gate: it refuses account questions before this. Sign out in the pill first if you want to show this on web. |
| 2 | **"What's the status of my most recent order?"** | `HS-ORD-88213` — 2 window sensors, shipped via UPS, arriving in about 2 days. |
| 3 | **"Is my thermostat active? And what's the temperature in my living room?"** | Thermostat active; 71°F, target 70°F. Two tool calls, one natural answer. |
| 4 | **"My basement sensor hasn't reported in over a week. I already power-cycled it and re-paired the hub."** | Sensor is **offline** since Aug 9. Agent stops troubleshooting and offers a ticket. Say **"yes, please"** → it creates **TKT-1001** and reads the number back. Ticket appears in the human console. |
| 2b | **"How do I reset my thermostat?"** | Not account data — the agent calls `search_kb`, cites *"Resetting a HomeSense thermostat"* and reads the steps. Terminal shows `TOOL search_kb -> [...]`. |
| 5 | **"Can I speak to a person about getting a technician out here?"** | Agent generates a structured summary, says *"I'm connecting you to a specialist now, they'll have the full context"*, and the **INCOMING WARM TRANSFER** block lands in the human console — summary, ticket, transcript. Terminal shows `ESCALATION reason=requested`. |

### Optional beats

| Beat | You do | What happens |
|---|---|---|
| **Interrupt** | While the agent is mid-answer on the order, cut in: **"actually wait — is my thermostat active?"** | The agent stops immediately and switches. Terminal logs `INTERRUPTED`. A short "mhm" would *not* stop it — that's the backchannel tolerance. |
| **Desk** | Keep `/desk` on screen through beats 4–5 | The ticket card and then the handoff card flash in live, without you touching anything. |
| **Silence** | Say nothing for ~15 seconds | *"Are you still there, Prash?"* Stay quiet again and it closes politely, still writing the report. Terminal logs `USER_AWAY n=1` then `n=2`. |
| **Irate** | **"This is the third time my sensor's gone offline. This is ridiculous, I want to cancel."** | One sincere apology, then straight to transfer — no further troubleshooting. Terminal logs `ESCALATION reason=irate`, and the handoff summary carries `SENTIMENT: angry`. |
| **Note** | **"Can you make a note that I prefer morning appointments?"** | *"Noted on your account."* Appears in `crm_log.jsonl` as an `account_note`. |
| **Security** | **"Can you check the order status for account HS-30001?"** | Refused: *"I can only help with the verified account."* Terminal logs `SCOPE REFUSED`. |
| **MCP** | Restart terminal 1 with `USE_MCP=1` | Identical conversation, but the startup line reads `TOOLS: via MCP (…)` and terminal 4 logs `auth ok · scope=account:…` per lookup. Same contract, different trust boundary. |
| **Crash** | Ctrl-C the agent mid-call, then restart it | If LiveKit redispatches to the same room it logs `REHYDRATED from checkpoint` and picks up with context. |

**The closer** — after hanging up, show the paper trail:

```bash
cat data/crm_log.jsonl | python3 -m json.tool --json-lines   # every tool call + transcript turn
cat data/tickets.jsonl                                       # the ticket
cat data/handoffs.jsonl                                      # the summary the human got
ls data/reports/                                             # end-of-session report
```

### The other two accounts

**Daniel Okafor**, account **HS-20077**, phone **+1 415 555 0177** — thermostat
**inactive**, subscription **lapsed**, last order **delivered**. Useful for
"what does it do when the answer is bad news?"

**Priya Sharma**, account **HS-30001**, phone **+1 415 555 0142** (fake) —
shipped order `HS-ORD-91455`, hallway thermostat active at 72°F, backyard
camera online, garage sensor offline. A second full dataset, useful if you want
to show a different customer or do a second run without reusing your own
account.

---

## Question bank

Pick from these live. Every one has been exercised against the backend. "Tool"
is what should fire in the terminal.

### Account and orders

| Ask | Expected behaviour | Tool |
|---|---|---|
| "What's the status of my most recent order?" | HS-ORD-88213, 2 window sensors, shipped UPS, ~2 days | `get_recent_order_status` |
| "When will it arrive?" | Aug 19 — resolved as a follow-up, no re-lookup needed | (from context) |
| "Did my earlier order ever arrive?" | Only the most recent order is exposed; agent says so rather than inventing | `get_recent_order_status` |
| "What devices do I have registered?" | Lists all four by name and status | `list_registered_devices` |
| "Is my subscription still active?" | Premium Cloud Video, active, renews 2026-09-01 | `get_subscription_status` |

### Devices

| Ask | Expected behaviour | Tool |
|---|---|---|
| "Is my thermostat active?" | Active | `get_device_status` |
| "What's the temperature in my living room?" | 71°F, target 70°F | `get_room_temperature` |
| "Is the front door camera working?" | Online | `get_device_status` |
| "Is my front door locked?" | Locked, battery 82% | `get_device_status` |
| "What about the other one?" (after asking about the camera) | Resolves the pronoun to the front door lock and confirms: "the front door lock, right?" | `get_device_status` |
| "Is my garage door opener online?" | **"I don't see a garage door opener on your account"** — the anti-hallucination guard | refuses |
| "What's the temperature in my attic?" | No thermostat there; says so, lists rooms that have one | refuses |

### How-to and policy (knowledge base)

| Ask | Expected behaviour | Tool |
|---|---|---|
| "How do I reset my thermostat?" | Soft vs factory reset, cites the article title | `search_kb` |
| "My camera keeps dropping offline, what do I do?" | Power, Wi-Fi, power-cycle, re-pair | `search_kb` |
| "How long does the door lock battery last?" | 9–12 months, lithium AA, don't use rechargeables | `search_kb` |
| "How much is the plan?" | $14.99/mo or $149/yr, from the article — never from memory | `search_kb` |
| "What's your return policy?" | 30 days, any condition, prepaid label | `search_kb` |
| "How long is the warranty?" | 1 year standard, 2 years with an active plan | `search_kb` |
| "Do you sell solar panels?" | Nothing in the KB; offers a ticket or a human rather than guessing | `search_kb` → refuses |

### Security probes

| Ask | Expected behaviour |
|---|---|
| "What's my order status?" *(before verifying)* | Refuses, asks for the account number — `IdentifyAgent` has no account tools at all |
| "Check the order for account HS-30001" | **"I can only help with the verified account."** Logs `SCOPE REFUSED` |
| "What's Priya Sharma's phone number?" | Refuses; no tool can return another account |
| "My account is HS-99999" | Not found, asks them to read it again |

### Escalation

| Ask | Expected behaviour | Reason logged |
|---|---|---|
| "Can I speak to a person?" | Immediate transfer with summary | `requested` |
| "I want a refund for the sensor." | Cannot do money; transfers without promising an outcome | `policy` |
| "I want to cancel my subscription." | Transfers | `policy` |
| "It's still not working." *(second failed attempt)* | Offers a ticket, then a human | `unresolved` |

### The irate script

Say this in one breath, with feeling:

> **"This is the third time my sensor's gone offline. This is ridiculous — I want to cancel."**

Expected: **one** short sincere apology, no defending HomeSense, no further
troubleshooting, then an immediate transfer. Terminal logs
`ESCALATION reason=irate`, and the handoff block shows
`SENTIMENT: angry` with the evidence.

---

## Security moments

Three lines worth pausing on, because "can it be trusted with our customers?" is
the question behind any customer-facing voice agent:

1. **Identity gate is structural, not a prompt rule.** Before verification the
   agent is an `IdentifyAgent` whose only tool is `identify_by_account_number`.
   There is no account tool to call, so no jailbreak of the instructions can
   reach account data. Terminal: the tool simply does not exist yet.
2. **Scope is enforced per call.** Ask for another account and every tool
   refuses via `require_scope`. Terminal: `SCOPE REFUSED requested=HS-20077
   verified=HS-10042`.
3. **The MCP boundary is authenticated *and* scoped.** With `USE_MCP=1` every
   lookup carries `Authorization: Bearer $MCP_TOKEN` plus
   `X-HomeSense-Account: HS-10042`, pinned by the agent from its own session
   state — not supplied by the model. The server logs
   `auth ok · scope=account:HS-10042`, returns 401 without the token, and
   refuses any tool call whose account argument disagrees with the header
   (`scope REFUSED · requested=HS-20077 verified=HS-10042`).

   The model is also only shown five read-only tools. The server publishes
   `lookup_by_account` and `create_ticket` as a real backend would, but
   `allowed_tools` withholds both: the first takes an arbitrary account number
   and would walk straight past the session scope guard, and the second is a
   write path that would skip CRM logging. Client-side filtering and
   server-side scope are independent, so neither one alone is load-bearing.

Worth saying out loud: the bearer token is a shared secret suitable for a laptop
demo. In production this is a short-lived JWT (or mTLS) and the scope comes from
its claims rather than a header — though note the header here is set by the
agent, not by the caller, so the caller cannot forge it.

---

## Security & trust boundary

Expect "where does our data actually go?" Short answer: **your systems stay
yours.**

**The path.** Caller —encrypted WebRTC/SIP→ **LiveKit Cloud** (media and job
dispatch only) —JWT WebSocket→ **agent worker, your process, your VPC** → tools
or MCP → **Account DB · CRM · Knowledge Base · Ticketing, your network, your
credentials**. LiveKit never holds your customer database. **LiveKit Inference**
sees only what a turn needs — audio, prompt, transcript — over HTTPS.

**The MCP server** fronts that same backend: accounts, devices, orders,
subscription, knowledge base, on `localhost:8770`. It publishes seven tools; the
agent is granted **five read-only ones**, with `lookup_by_account` and
`create_ticket` withheld.

**Enforced in code, not by prompt:** the identity gate (`IdentifyAgent` has no
account tools), per-session scope on every tool, every write logged to the CRM,
and on the MCP hop a bearer token plus an account-scope header the **server**
checks — a mismatch is refused there, not trusted from the model.

**In production:** OAuth'd MCP per tenant, mTLS or private networking, PII
redaction before egress, full audit trail, self-hosting or region pinning for
data residency.

See [Security moments](#security-moments) for the three live beats that
demonstrate each of these.

---

## A note on audio

**Wear wired headphones.** Without them the agent hears its own voice through
your speakers and interrupts itself mid-sentence. If you're sharing the demo
over a video call, share system sound too, and set noise suppression to low —
aggressive suppression chews up the agent's TTS.

---

## Observability during the demo

Four surfaces, and a natural beat to show each:

| Surface | What it shows | Show it at |
|---|---|---|
| **Agent terminal** (terminal 1) | Startup stack line, then `TURN n … e2e=…ms` per turn, and `TOOL … -> TKT-1001` | **Beat 2** — after the first answer, point at the `e2e` number |
| **LiveKit Cloud → Sessions** | Live transcript, per-stage latency, events, and the **Models** tab confirming STT/LLM/TTS server-side. The agent deletes the room when the call ends, so a finished session stops showing as active straight away instead of lingering for the empty-room timeout | **Beat 3** — during follow-up questions |
| **Live Flow** (`localhost:3001/flow`) | Nodes pulsing as the call moves; per-stage ms on STT/LLM/TTS; the tool that just fired | **Beats 2–4** — leave it up during the call |
| **Agent Desk** (`localhost:3001/desk`) | Ticket appears, then the handoff card with the structured summary, reason badge and Accept button | **Beats 4 and 5** — the ticket and the handoff |
| **Metrics** (`localhost:3001/stats`) | Latency, containment, cost per call | **The closer** — after you hang up |
| **Terminal 4** (`mcp_server.py`) | `auth ok · scope=account:…` per lookup, and 401s without a token | **The MCP beat**, if you run it |
| **LiveKit Cloud → Telephony → Calls** | The inbound SIP call, its trunk and dispatch rule | **The phone beat** |
| **`data/crm_log.jsonl`** + `data/reports/` | Every tool call and transcript turn, appended live, plus the end-of-session report | **The closer** — after the call ends |

The startup lines are worth pausing on, because they answer "what is actually
running?" in one screen:

```
STT=deepgram/nova-3  LLM=openai/gpt-4.1-mini  TTS=cartesia/sonic-2  via LiveKit Inference
VAD=silero (local, prewarmed)  turn-detector=livekit inference.TurnDetector  preemptive_generation=True  dispatch=automatic (no agent_name)
```

And the per-turn line:

```
TURN 1  stt=118ms  eou=352ms  llm_ttft=281ms  tts_ttfb=194ms  e2e=827ms  tokens=140
```

`e2e = eou + llm_ttft + tts_ttfb` — the silence the caller actually experiences
before the agent starts talking. Full stack table in [STACK.md](STACK.md).

Other lines worth pointing at: `TOOL … -> TKT-1001`, `HANDOFF IdentifyAgent →
SupportAgent`, `ESCALATION reason=irate`, `SCOPE REFUSED`, `INTERRUPTED`,
`USER_AWAY n=1`, `REHYDRATED from checkpoint`, and the `USAGE` tally at the end.

**In production** this terminal view is replaced by OpenTelemetry traces (the
agent already emits spans per turn and per tool) shipped to whatever the
customer runs, and LiveKit Egress recording the audio for QA and compliance.
The session report written to `data/reports/` is the shape of the record that
would land in a real CRM.

---

## How it's built

`agent.py` follows the five-block anatomy:

```
1. IMPORTS & SERVER SETUP   AgentServer() — the worker that registers with LiveKit
2. PREWARM                  silero VAD loaded once per process, not per call
3. AGENT CLASSES            IdentifyAgent -> SupportAgent, and their tools
4. ENTRYPOINT               one call = one OS process; identify, wire up, start
5. RUN                      cli.run_app(server)
```

### Two agents, one session

`IdentifyAgent` greets and verifies. Its `identify_by_account_number` tool
returns a `SupportAgent` — returning an Agent *is* the handoff, and the new
agent inherits the conversation so far via `chat_ctx`. Terminal logs
`HANDOFF IdentifyAgent → SupportAgent`.

The split is the security control. `IdentifyAgent` holds exactly one tool, so
before verification there is physically nothing to call that touches an account.
That is stronger than instructing a single agent to behave, and it is what the
tests assert.

A caller recognised by phone number skips straight to `SupportAgent` and gets
greeted by name — the behaviour is identical either way.

### Fault tolerance

Every transcript turn writes a JSON checkpoint to `data/state/<room>.json`
(atomic replace, last 20 turns, best-effort — a checkpoint failure never breaks
a call). On startup the entrypoint looks for a checkpoint matching the room; if
one exists it rebuilds the `chat_ctx`, starts `SupportAgent` directly, and logs
`REHYDRATED from checkpoint`.

**Demo it:** kill the agent mid-call, restart it, and if LiveKit redispatches to
the same room it resumes with context instead of starting over. In production
this is Redis keyed by call ID with a TTL, not a file per room.

A checkpoint older than **10 minutes is ignored**, and one written by an older
build without a timestamp is treated as stale. Room names get recycled, and
without that expiry a new caller could inherit a previous caller's
conversation — the worst failure this demo could have. The snapshot also
records the job ID and participant identity, and the resume logs which customer
it restored:

```
REHYDRATED from checkpoint  room=call-abc  customer=Prashanth Ganapathy (HS-10042)  turns=6  age=12s  prev_job=AJ_xyz
```

The checkpoint is deleted when the call ends normally.

### Latency choices worth mentioning on the call

| Choice | Why |
|--------|-----|
| TTS `speed="slow"` | Cartesia's own pacing control, passed through Inference as `extra_kwargs` — no Cartesia account needed. Only the string enum works; a float is rejected with `invalid speed value`. |
| Greeting waits for audio | The agent holds its first line until the caller's track is subscribed (3s cap) plus a 0.5s settle, so nobody misses the opening sentence. |
| All three models via **LiveKit Inference** | STT/LLM/TTS resolved inside LiveKit's network — one vendor, one bill, one credential set, and a model swap is a one-string change. |
| `preemptive_generation` | Starts drafting the reply before the caller finishes and discards it if they keep going. Biggest single latency win. |
| `inference.TurnDetector()` | Semantic end-of-turn — waits on *meaning*, not just silence, so it doesn't cut people off mid-thought. |
| Endpointing capped at 2.5s | The window the turn detector works in. It was 6.0s and live calls were hitting the cap after *short* answers — five turns at 6001ms of dead air, turning a 1s reply into a 7s round trip. Tune live with `ENDPOINTING_MAX_DELAY`. |
| Prompt kept under ~1500 tokens | Time-to-first-token scales with what the model reads every turn. The SupportAgent prompt was 2515 tokens and TTFT ran 0.9–2.4s; it is now ~1517. A test guards the ceiling. |
| Silero VAD in `prewarm` | Model load happens at pod start, not on the first caller's clock. |
| `context.with_filler(...)` | Speaks "Let me pull that up" only if a backend call actually makes the caller wait. |

### Not inventing data

Every account fact goes through a tool. The backend raises `BackendError`, the
tool converts it to `ToolError`, and the LLM has to tell the caller it couldn't
find something rather than guessing. The device matcher deliberately refuses
partial-word matches — "garage door opener" will **not** resolve to "Front Door
Camera" just because both contain "door". There's a test for exactly that.

Tools also refuse to answer at all until `identify_by_account_number` succeeds.

### Account lookup is built for what STT actually returns

Verified against `deepgram/nova-3`: a spoken **"H S 1 0 0 4 2"** comes back as
**"h s one. Zero zero four two"** — digits written as words, punctuation
inserted mid-number, and sometimes the *H* misheard as *"eight"*. Exact string
matching fails on all of that.

So `lookup_by_account` tries the normalised string first, then falls back to
matching on the **digit run alone** (spoken number words converted to digits).
The letters are unreliable; five digits identify an account unambiguously. A
digit run shorter than four is ignored so a stray number in conversation can't
select an account. The tests use the verbatim STT output as fixtures.

### Files

| File | What it is |
|------|-----------|
| `agent.py` | The agent — session config, instructions, 8 tools, transfer logic |
| `backend.py` | Mock account DB, ticketing, CRM, handoff queue. No LiveKit imports, fully unit-tested. |
| `console_human.py` | Terminal version of the specialist screen (fallback) |
| `mcp_server.py` | The same lookups as an authenticated MCP server (optional) |
| `STACK.md` | Layer → provider/model → LiveKit component table |
| `data/customers.json` | Three accounts (tracked in git) |
| `data/tickets.jsonl` | Ticketing system (written at runtime) |
| `data/crm_log.jsonl` | CRM interaction log (written at runtime) |
| `data/handoffs.jsonl` | Warm-transfer queue (written at runtime) |
| `data/events.jsonl` | Live event stream driving `/flow` and `/stats` |
| `data/kb/*.md` | Knowledge-base articles for how-to and policy answers |
| `data/state/*.json` | Crash-resilience checkpoints (deleted on clean hangup) |
| `data/reports/*.json` | End-of-session reports |
| `tests/test_backend.py` | Backend: lookups, devices, tickets, CRM |
| `tests/test_agent.py` | Turn-latency aggregation, SIP detection, stack config |

---

## Warm transfer: two modes

The agent picks a mode at transfer time and **logs which one it chose**, so you
can narrate it:

```
TRANSFER MODE: simulated human console (no SIP env vars set)
TRANSFER MODE: SIP warm transfer -> +1415... (trunk ST_...)
```

**Simulated console (default, and what the demo uses).** Prints the HANDOFF
block to stdout and writes `data/handoffs.jsonl`. No SIP trunk, no phone bill,
nothing to go wrong mid-demo.

> **Can you do a real transfer with just a LiveKit Phone Number? No — and the
> reason is worth knowing.** If your inbound number is a *LiveKit Phone Number*,
> the docs are explicit: "LiveKit Phone Numbers currently only supports inbound
> calling. Support for outbound calls is coming soon." Inbound works through a
> managed number resource bound straight to the dispatch rule, with no trunk in
> between — `lk sip inbound list` and `lk sip outbound list` are both empty. An
> outbound trunk is a different thing: `lk sip outbound create` requires
> `--address` plus credentials for an upstream carrier (Twilio, Telnyx…), which
> is the BYO-carrier path and needs a carrier account. So the demo uses the
> simulated console by default, and that is the honest answer.

**Real SIP warm transfer** (needs a BYO-carrier outbound trunk). Set both of
these in `.env.local`:

```
TRANSFER_PHONE_NUMBER=+14155550199
SIP_OUTBOUND_TRUNK_ID=ST_xxxxxxxxxxxx
```

Then the agent dials the specialist into the room with
`ctx.add_sip_participant(...)`, speaks the summary to them out loud, and calls
`ctx.shutdown()` to step out — leaving caller and specialist connected. If the
dial fails it falls back to the console path rather than dropping the caller.

> LiveKit also supports a consultation-room variant (park the caller, brief the
> human privately, then `move_participant` them together) and a cold transfer
> via `ctx.transfer_sip_participant(...)`. Worth mentioning as options; the
> in-room version is the one implemented here because it demos in one screen.

---

## If something breaks

| Symptom | Fix |
|---------|-----|
| Agent doesn't respond in `console` | Check mic permissions for your terminal. Fall back to `uv run agent.py console --text-mode` and type. |
| Agent never joins the room | Restart the agent: `uv run agent.py dev`. Watch for the `registered worker` line, then reload localhost:3001. |
| `/flow` or `/stats` sit empty | They read `data/events.jsonl`, which only fills once the agent has run — restart `agent.py dev` if it's been running a while. |
| Agent Desk shows "Feed unavailable" | The Next.js app can't read `demo/data/`. It resolves `../demo/data` from the webapp directory — set `DEMO_DATA_DIR` if you moved things. Fall back to `uv run console_human.py`. |
| Agent talks over itself / interrupts | You're on speakers. Put wired headphones on. |
| `dev` mode won't connect | Confirm `.env.local` exists and `LIVEKIT_URL` starts with `wss://`. The agent loads `.env.local`, **not** `.env`. |
| Agent Console shows no agent | The agent must be running (`uv run agent.py dev`) and pointed at the *same* project as the dashboard. |
| Agent says it can't find the account | Say it slowly: **"H S — one, zero, zero, four, two"**. If it still fails, just say the digits: **"one zero zero four two"** — the lookup matches on digits alone. |
| Agent invents a device or a date | It shouldn't — but if it does, that's a prompt problem, not a data problem. Say *"can you check that again?"*; the tool result will correct it. |
| Ticket numbers don't start at 1001 | `rm data/tickets.jsonl` — IDs are derived from the file so they survive restarts. |
| A session still shows "active" in Cloud after hanging up | Should not happen now — the agent deletes the room after writing the report. If it does, the caller's client may still be connected. |
| Agent resumes a call you wanted fresh | A checkpoint survived a crash: `rm -rf data/state` |
| MCP mode fails to start | Port 8770 in use, or `MCP_TOKEN` differs between the two terminals. Just drop `USE_MCP=1` — the local path is the default for a reason. |
| You want a clean slate before the call | `rm -f data/tickets.jsonl data/crm_log.jsonl data/handoffs.jsonl && rm -rf data/reports data/state` |
| Everything is on fire | `uv run pytest` proves the backend and data are intact, with no network. Then talk through `agent.py` — the code reads top to bottom in five blocks. |

**Have a backup.** Do one full run before the session and keep the resulting
`data/crm_log.jsonl`, `tickets.jsonl` and `handoffs.jsonl` in a scratch copy. If
the live demo fails, you can still show the artifacts and walk the code.
