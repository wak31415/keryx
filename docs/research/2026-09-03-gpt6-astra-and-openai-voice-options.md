# GPT-6 Astra and the OpenAI voice/agent stack — what it means for Jarvis

**Date:** 2026-09-03 · **Task:** Jarvis-Task 81 · **Verdict: change almost nothing.**

Everything below marked **[verified]** was checked against the live API with William's own
key on 2026-09-03, not read off a blog. Everything marked **[docs]** comes from
`developers.openai.com`. Press coverage is used only for the launch narrative.

---

## 0. The one-paragraph answer

GPT-6 Astra is a **text-in/text-out reasoning model**. It has **no Realtime API support**,
so it cannot be the voice of Jarvis — not now, not as currently specified. It is a
candidate to replace the *subagent* brain, but Jarvis's subagents run on a flat-rate Claude
subscription, so that swap would turn near-zero marginal cost into metered spend at
$10/$50 per million tokens, and would mean abandoning the Claude Agent SDK — which is what
the skills, the MCP wiring and the entire approval bridge are built on. Meanwhile Jarvis is
**already on the newest realtime model** (`gpt-realtime-2.1`). The only thing on this whole
list that actually needs doing is a transcription model that shuts down in February 2027.

---

## 1. What was announced, and what it is not

OpenAI began rolling out **GPT-6 Astra** on 2026-09-03. Greg Brockman called it a
"generational leap"; Axios reports OpenAI saying it may represent AGI. It is the first
model OpenAI has designated as reaching its **"Critical" cybersecurity capability
threshold**, which is why the launch came wrapped in safeguards rather than a plain
availability announcement.

### The specification that decides this whole question **[docs]**

| Property | Value |
|---|---|
| Model id | `gpt-6-astra` |
| Context window | 1,050,000 tokens (922k in / 128k out) |
| **Input modalities** | **text, image** |
| **Output modalities** | **text only** |
| Supported APIs | Chat Completions, Responses |
| **Realtime API** | **Not supported** |
| `reasoning.effort` | `low` · `medium` · `high` · `xhigh` · `max` |
| Knowledge cutoff | 2026-04-30 |
| Price | $10/M in · $1/M cached · $50/M out |
| Long-context penalty | >272k input tokens ⇒ **2× input, 1.5× output on the whole request** |
| Tier 5 limits | 15,000 RPM / 40M TPM |

> **This is the finding that answers question 1.** Astra has no audio path in either
> direction. Any headline implying "GPT-6 powers voice agents" is wrong about the API.
> A voice agent can only use Astra as a *back-end reasoner behind* a speech model.

### Availability — checked, not assumed **[verified]**

```
GET /v1/models/gpt-6-astra   →  HTTP 404
{"code": "model_not_found", "message": "The model 'gpt-6-astra' does not exist"}
```

126 models are visible on his key. **None** match `gpt-6` or `astra`. Rollout is staged:
cybersecurity-program participants first, then ChatGPT Plus/Pro/Business/Enterprise, then
the API "in the next few days", plus AWS. There is nothing to migrate to yet, and no date
he can plan against.

### Safety posture, and why it matters to an agent with a shell

The safeguards are not cosmetic, and two of them have direct architectural consequences
for anything that runs Astra agentically:

- **Universal monitoring across all agentic applications of Astra.** Monitors evaluate the
  model's chain of thought and can **trigger a security response that interrupts high-risk
  activity**. Jarvis's subagents run with `permission_mode="bypassPermissions"`, full Bash,
  Edit and Write, and reach into a Slurm cluster. That is a legitimate workload that
  nonetheless looks a lot like the silhouette these monitors are built to catch. An
  agent that can be interrupted mid-task by a vendor-side classifier is a different
  reliability proposition from one that cannot.
- **Tiered access for cyber capability** via the Daybreak Blue / Daybreak Red tiers
  (shipped 2026-08-07), with an alpha group first. Not something he qualifies for or needs.

## 2. The realtime voice lineup — he is already at the top of it

**[verified]** `gpt-realtime-2.1`, `gpt-realtime-2.1-mini`, `gpt-realtime-2`,
`gpt-realtime-1.5`, `gpt-realtime-translate`, `gpt-realtime-whisper`, `gpt-transcribe`,
`gpt-live-transcribe` are all present on his key.

`.env.example` / `config.py` already pin **`OPENAI_REALTIME_MODEL=gpt-realtime-2.1`** —
the current flagship. There is no upgrade available to him.

| | `gpt-realtime-2.1` (his) | `gpt-realtime-2.1-mini` |
|---|---|---|
| In / Out | text, audio, image → text, audio | same |
| Context | 128k (32k out) | 128k |
| Audio in / out per M | **$32 / $64** | **$10 / $20** |
| Text in / cached / out | $4 / $0.40 / $24 | lower |
| Image in | $5 | — |
| Reasoning effort | configurable | configurable |
| Endpoint | `v1/realtime` only | `v1/realtime` only |

Rate limits for `gpt-realtime-2.1` **[docs]**: T1 200 RPM/40k TPM · T2 400/200k ·
T3 5,000/800k · T4 10,000/4M · T5 20,000/15M. His observed headers were
**500 RPM / 200,000 TPM** **[verified]**, consistent with **Tier 2**. For a single-user
phone agent this is not remotely a constraint — but note it *would* be the binding limit
long before Astra's own limits mattered.

**Realtime feature notes relevant to Jarvis:**

- Tool types in a realtime session are **function tools, MCP servers and connectors**.
  This corroborates the comment in `web_search.py` ("only `function` and `mcp`",
  verified 2026-08-24) — still accurate. There is still **no hosted `web_search`** in
  realtime, so the Responses-API proxy he wrote remains the correct design.
- MCP tools in realtime are **executed by the API itself**, not by his client.
- Transports: WebSocket (his path), WebRTC, and **SIP** for telephony.
- Docs guidance: *"start with `reasoning.effort` set to `low` for most production voice
  agents."* `SessionConfig` does not currently set it at all — see §5.

### Nothing shipped for voice on 2026-09-03

The realtime refresh was **2026-05-08 / 09-01** (`gpt-realtime-2` + translate + whisper).
The Astra launch contained **no voice or realtime component**. The changelog for Sep 3 is
Astra itself plus Responses-API controls (below).

## 3. Agent-side and orchestration

- **The Assistants API shut down on 2026-08-26** **[docs]** — replacement is the
  Responses + Conversations APIs. Jarvis never used it, so this costs him nothing. Worth
  knowing only because half the "OpenAI agents" writing online still assumes it exists.
- **AgentKit** (Agent Builder, ChatKit, Evals) and the **Agents SDK** — the SDK's
  2026-04-15 release added configurable memory, sandbox-aware orchestration and
  Codex-like filesystem tools. This is a genuine competitor to the Claude Agent SDK, and
  it is also the *only* way to get Astra's agent loop. See §4 for why that trade is bad.
- **New Responses API controls, shipped 2026-09-03 alongside Astra** **[docs]**:
  - **Async tool calling** — the model keeps working while your tool runs, with a `wait`
    tool to block only when it actually needs the result. **Requires GPT-6 Astra or later,
    Responses API only. Not available in Realtime.**
  - **Mid-turn steering** — inject instructions over a WebSocket while a response is
    already in flight.
  - **Change `reasoning.effort` mid-conversation** while preserving cached prompts.
  - WebSocket multiplexing: parallel conversations and conversation forking on one socket.

> **Tempting and wrong:** async tool calling looks like the fix for `dispatch_task`
> holding the line while a subagent runs (`inline_waits.py`). It is not — it is
> Responses-API-only and Astra-only, and Jarvis's voice turn happens in a Realtime
> session. The existing `inline_waits` + Notifier handshake stays the right mechanism.

## 4. What his money actually looks like

**[verified]** — real spend, from the org costs endpoint, last 30 days:

```
Last 30 days total: $11.94
   5.03  gpt-realtime-2.1 audio, output
   3.00  gpt-realtime-2.1 text, input
   1.34  gpt-realtime-2.1 audio, input
   1.22  gpt-realtime-2.1 text, output
   1.19  gpt-realtime-2.1 text, cached input
   0.05  gpt-4o-mini-transcribe audio, input
   0.03  web search tool calls
   0.01  gpt-5.4-mini (web_search proxy)
```

Two things fall out of this:

1. **Twelve dollars a month, and 97% of it is the voice model.** Any optimisation that is
   not about realtime audio tokens is rounding error. `gpt-5.4-mini` powering `web_search`
   costs him roughly one cent a month; there is no case for touching it.
2. **`text, input` at $3.00 is the second-largest line, and only $1.19 is cached.** That
   is the briefing — instructions, memory, task digest, tool schemas — re-sent at $4/M on
   every socket, because a realtime session starts blank. This is the single largest
   *addressable* cost in the system, and the lever is prompt caching, not a new model.

**And the decisive fact for the Astra question** **[verified]**:

```
ANTHROPIC_API_KEY:            unset
CLAUDE_CODE_OAUTH_TOKEN:      unset
~/.claude/.credentials.json:  present  →  subagents run on the flat-rate subscription
```

Subagent compute is currently **flat-rate**. Moving subagents to Astra converts that into
metered billing at $10/$50 per M — and subagents are by far the most token-hungry part of
Jarvis. This is not a marginal cost difference; it is the difference between a fixed
monthly fee and an open-ended one.

## 5. The only thing that actually needs doing

**`gpt-4o-mini-transcribe` shuts down 2027-02-26** **[docs]**, along with `whisper-1`,
`gpt-4o-transcribe` and `gpt-4o-transcribe-diarize`. It is hard-coded in two places:

- `src/jarvis/realtime/base.py` — `SessionConfig.transcription_model` default
- `src/jarvis/config.py:107` + `.env.example` — `OPENAI_TRANSCRIPTION_MODEL`

The documented replacements are `gpt-transcribe` ($0.0045/min) or `gpt-live-transcribe`
($0.017/min). **This is not a clean drop-in, and the docs contradict themselves:**

- The model pages for **both** replacements list `v1/realtime` as **"Not supported"**,
  claiming only `v1/audio/transcriptions` and `v1/realtime/transcription_sessions` work.
- But the same `gpt-transcribe` page also says it handles *"committed turns in Realtime
  sessions over WebSocket"* — which is exactly what input transcription in a
  speech-to-speech session **is**.
- **[verified]** A realtime session created with each of the three as
  `audio.input.transcription.model` returns **HTTP 200** for all three.

So config-time acceptance is confirmed, but **I did not push audio through the socket** —
the test suite forbids network access and I was not going to place a live call to find
out. Treat `gpt-transcribe` as the likely replacement, **confirmed by one live call**
before the default changes. It is also ~4× cheaper per minute than `gpt-live-transcribe`,
and transcription is $0.05/month of his bill either way, so cost is not the deciding factor —
correctness on the wire is.

**Deadline is ~6 months out. This is not urgent, and it should not be rushed into a
commit that a live call has not backed.**

Other deprecations, none of which touch him: `gpt-realtime`/`gpt-4o-realtime`/
`gpt-realtime-mini` shut down 2027-01-20; `gpt-5-2025-08-07` on 2026-12-11.

---

## 6. Options

### Option A — Hold. Change nothing but the transcription model. **← recommended**

- **Pros:** He is already on the best available voice model. Astra cannot do voice and
  isn't on his account anyway. Subagents stay flat-rate. Zero migration risk to a system
  that works and that he depends on daily.
- **Cons:** None that are real. "Not using the new model" is not a cost when the new model
  cannot do the job.
- **Work:** one line, after one live call. Plus optional §7 tuning.

### Option B — Keep the voice, move subagents to Astra via the Agents SDK

- **Pros:** Frontier reasoning for hard tasks; 1.05M context; async tool calling; one vendor.
- **Cons:** Throws away the Claude Agent SDK and with it the skills, the subagent-of-its-own
  recursion, and **the entire approval bridge**, which is built on Claude Code's hook
  system (`PermissionRequest`/`PostToolUse`/`Stop`/`SessionEnd`) and has no OpenAI
  equivalent. Converts flat-rate compute to metered. Adds vendor-side chain-of-thought
  monitoring that can interrupt a legitimate long-running task. **And the model is not
  available on his account, so this cannot even be prototyped today.**
- **Verdict:** the cost is measured in weeks and the benefit is speculative. No.

### Option C — Astra as an escalation path *behind* Claude, for a narrow class of task

- Keep everything. Add an optional tool a subagent may call for a genuinely hard reasoning
  problem, answered by Astra over the Responses API. The `codex` skill already installed on
  this machine is the same idea and already works.
- **Pros:** Bounded blast radius, no architectural change, easy to delete.
- **Cons:** Metered spend; needs access he does not have; solves a problem he has not
  reported having.
- **Verdict:** revisit *if* Astra lands on his key **and** he hits a task Opus 5 fails.

### Option D — `gpt-realtime-2.1-mini` for the voice

- ~3× cheaper audio ($10/$20 vs $32/$64) — would cut the bill from ~$12 to maybe ~$5.
- **Cons:** Saves seven dollars a month in exchange for a worse model on the one component
  where quality is the entire product. 2.1 specifically improved **alphanumeric
  recognition** — order numbers, confirmation codes, PINs — and Jarvis is **PIN-gated over
  a phone line**. Degrading digit recognition to save pocket change is a bad trade.
- **Verdict:** no. Worth knowing it exists if usage ever grows by two orders of magnitude.

## 7. Suggested default path

**Phase 0 — now, no code.** Nothing to do about Astra. It is text-only and not on his
account. Re-check `GET /v1/models/gpt-6-astra` in a few weeks; the 404 is the whole status.

**Phase 1 — the deprecation, on its own merits (before ~Jan 2027).**
Set `OPENAI_TRANSCRIPTION_MODEL=gpt-transcribe` in `.env`, place **one real call**, and
confirm transcripts still arrive (`jarvis tasks list`, the transcript store). Only then
change the default in `config.py` and `base.py`, update `.env.example`, and commit. If
transcripts break, `gpt-live-transcribe` is the fallback at 4× the price.

**Phase 2 — the actual cost lever, if he wants one.**
`text, input` is $3.00/month against $1.19 cached. Look at whether `briefing.py` assembles
its prompt with a stable prefix — instructions and tool schemas first, volatile digest and
memory last. Realtime supports prompt caching at $0.40/M vs $4/M. This is a bigger and
more certain win than any model swap on this page, and it touches no model id.

**Phase 3 — free tuning, worth an experiment.**
The realtime guide recommends `reasoning.effort: low` for production voice agents.
`SessionConfig` never sets it, so Jarvis takes the server default. Adding it as a settable
field is a small change that may cut time-to-first-word. Try it on the local channel where
a bad call costs nothing.

**Explicitly keep as-is:** `gpt-realtime-2.1` · the Claude Agent SDK and
`claude-opus-5` subagents on the subscription · the `web_search` Responses-API proxy
(one cent a month, and realtime still has no hosted search) · the approval bridge · the
`inline_waits` handshake · the whole Twilio/WebSocket transport.

---

## Sources

Astra: [model docs](https://developers.openai.com/api/docs/models/gpt-6-astra) ·
[CNBC](https://www.cnbc.com/2026/09/03/open-ai-astra-gpt-6-cyber.html) ·
[Axios (AGI framing)](https://www.axios.com/2026/09/03/openai-astra-gpt-6-agi-brockman) ·
[Axios (cyber limits)](https://www.axios.com/2026/09/01/openai-astras-cyber-critical) ·
[Bloomberg](https://www.bloomberg.com/news/articles/2026-09-03/openai-rolls-out-gpt-6-astra-model-with-added-cyber-guardrails) ·
[The Hill](https://thehill.com/policy/technology/6065937-openai-astra-requires-stronger-safeguards/) ·
[Path to Astra](https://openai.com/index/path-to-astra/) ·
[Frontier cyber capabilities](https://openai.com/index/responding-next-frontier-critical-cyber-capabilities/)

Voice/realtime: [gpt-realtime-2.1](https://developers.openai.com/api/docs/models/gpt-realtime-2.1) ·
[realtime guide](https://developers.openai.com/api/docs/guides/realtime) ·
[realtime + MCP](https://developers.openai.com/api/docs/guides/realtime-mcp) ·
[gpt-transcribe](https://developers.openai.com/api/docs/models/gpt-transcribe) ·
[gpt-live-transcribe](https://developers.openai.com/api/docs/models/gpt-live-transcribe) ·
[voice models launch](https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/)

Agents/platform: [changelog](https://developers.openai.com/api/docs/changelog) ·
[deprecations](https://developers.openai.com/api/docs/deprecations) ·
[async tool calling](https://developers.openai.com/api/docs/guides/async-tool-calling) ·
[mid-turn steering](https://developers.openai.com/api/docs/guides/steering) ·
[WebSocket mode](https://developers.openai.com/api/docs/guides/websocket-mode) ·
[Agents SDK](https://developers.openai.com/api/docs/guides/agents) ·
[AgentKit](https://openai.com/index/introducing-agentkit/) ·
[GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
