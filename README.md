# Jarvis

[![CI](https://github.com/wak31415/garmin-voice-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/wak31415/garmin-voice-agent/actions/workflows/ci.yml)

A personal voice agent you can reach two ways:

- **By phone** — call a Twilio number (from a watch, a car, anywhere). The call opens a
  realtime voice session.
- **Locally** — say "hey jarvis" to a Mac's microphone and the same session opens
  without a phone.

Speech goes through the **OpenAI Realtime API** (speech-to-speech, server VAD, function
calling), so the conversation stays snappy. Anything that is real work — research, a code
change, mail and calendar chores — is handed to a **Claude Agent SDK subagent** that runs
on the host machine with full local access and reports back by voice, SMS, or a call
back.

The two channels can live on one machine or two. The phone channel runs anywhere —
in practice a Linux box that is up 24/7, reached through a Cloudflare tunnel — while the
wake word needs macOS, because openwakeword cannot be installed on Linux under Python
3.12 (its `tflite-runtime` dependency has no cp312 wheel). A Linux host therefore serves
with `--no-wakeword`.

The full design lives in
`docs/superpowers/specs/2026-08-18-jarvis-voice-agent-design.md`.

## Architecture

```
Phone/Watch ─PSTN─▶ Twilio ─WSS media stream─▶ Cloudflare Tunnel ─▶ FastAPI (Linux)
                                                          │
Mac mic ── openWakeWord "hey jarvis" ──▶ LocalAudioDevice / LocalTransport   (macOS)
                                                          ▼
                                    VoiceSession  (transport-agnostic core)
                               audio pump ▲▼   tool calls   ▲ announce()
                                          │                 │
                            OpenAIRealtimeClient        Notifier ◀── EventBus
                                          │                 ▲
                                    ToolRegistry ──▶ TaskManager ──▶ Claude Agent SDK
                                                          │  (one AgentSession per task)
                                                          ▼
                                             SQLite tasks  +  Twilio SMS / call-back
```

One `VoiceSession` drives any transport: Twilio media streams (µ-law 8 kHz, passed
through untouched), the local mic/speaker (16-bit PCM 24 kHz, half-duplex — the mic is
gated off while Jarvis speaks), or a WAV file for the `loopback` dev harness. The session
never knows which one it has.

Tools the model can call: `web_search`, `send_to_slack`, `dispatch_task`, `list_tasks`, `get_task_status`,
`get_task_result`, `send_followup`, `cancel_task`, `list_projects`, `request_callback`,
`restart_service`, `submit_pin`, `end_session`.

Slack is opt-in: nothing goes to it unless you asked for it. When you do ask, the voice
sends text with `send_to_slack` and subagents send files, plots and reports through the
same Slack app, which they already have from the `auto-research` skill. Unasked, a file
stays in the written report — Jarvis tells you it is there and offers to send it, rather
than reading a path down the phone.

There is exactly one routing decision. Small talk, task status and small factual questions
the voice answers itself — `web_search` goes through the Responses API, because a Realtime
session has no hosted search tool of its own. Everything else becomes a task, and a task is
just "Claude, on this machine": one kind, every tool, the repositories, Gmail and Calendar,
the installed skills, and subagents of its own. Nothing classifies the work in advance.

## Setup

### Prerequisites

- macOS or Linux with Python 3.12 and [`uv`](https://docs.astral.sh/uv/) — the wake
  word is macOS-only, the phone channel runs on either
- **OpenAI API key** with Realtime access
- **Claude subscription login** (`claude /login`, once) — subagents run on it by default.
  Alternatives: `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` (headless/launchd), or an
  `ANTHROPIC_API_KEY` (pay-per-token; takes precedence when set)
- The **`claude` CLI** — the Agent SDK drives it, and ships a bundled copy it prefers
  over `PATH`. Install one yourself (`npm i -g @anthropic-ai/claude-code`) only if
  `jarvis doctor` says there is neither
- A **Twilio** account, a phone number, and a **Cloudflare Tunnel** with a hostname
  routed to it — `cloudflared` plus a zone on Cloudflare (for the phone channel)
- Gmail and Calendar need nothing: they come from the Claude CLI's claude.ai connectors

### Install

```bash
uv sync
cp .env.example .env      # then fill it in (see the table below)
uv run jarvis download-models   # macOS only: fetches the openWakeWord "hey jarvis" model
uv run jarvis doctor            # tells you what is still missing
```

### Configuration

Every setting is an environment variable, read from `.env` in the working directory.
`.env.example` lists them all; the ones you must set are:

| Env | What it is |
|---|---|
| `OPENAI_API_KEY` | Realtime API key (required) |
| `ANTHROPIC_API_KEY` | optional — pay-per-token override for subagent auth |
| `CLAUDE_CODE_OAUTH_TOKEN` | optional — subscription token from `claude setup-token` |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_NUMBER` | phone channel + outbound SMS/calls |
| `ALLOWED_CALLERS` | comma-separated E.164 numbers allowed to call in — everything else is refused |
| `JARVIS_PIN` | PIN for destructive work over the phone (`coding`, `cowork`) |
| `PUBLIC_HOST` | the tunnel hostname Twilio reaches, e.g. `jarvis.example.com` |
| `PROJECTS` | JSON map of spoken project names to repo paths, e.g. `{"jarvis": "/Users/me/code/jarvis"}` |

A project can also introduce itself: put a **`.jarvis-brief.md`** at its root and the voice
prompt carries it verbatim — what the project is, what the jargon means out loud, what
state it is in. Keep it short (it is capped at 1500 characters). Do not point this at a
repository's `CLAUDE.md`: that is thousands of tokens of build detail written for a screen,
the subagent reads it for itself anyway, and in a voice prompt it mostly drowns the persona.

**If Jarvis cuts you off while you think**, that is turn detection. It defaults to
`VAD_MODE=semantic` with `VAD_EAGERNESS=low`, which waits on whether your sentence sounds
finished rather than on a stopwatch — the most patient setting. If it feels sluggish
instead, `VAD_MODE=server` with `VAD_SILENCE_MS` (default 1200) goes back to a fixed timer
you can tune directly.

Useful optional ones: `HOST`/`PORT` (default `127.0.0.1:8080`), `DATA_DIR` (default
`~/.jarvis`), `PROJECTS_ROOT` (every subdirectory is dispatchable by name), `SKILLS_DIR`
(default `~/.claude/skills`, listed in the voice prompt), `SUBAGENT_MODEL` (default
`claude-opus-5`), `LOG_LEVEL`, `SERVICE_MANAGER`/`SERVICE_UNIT` (which unit
[`jarvis restart`](#restarting-it) asks to restart; `auto` finds it), and the guardrails
below. The full table is spec §3.4.

### Cloudflare tunnel (for the phone channel)

Twilio has to reach this machine, and the tunnel is the only thing exposed. With the
hostname's zone on Cloudflare, create the tunnel once — it opens a browser to authorize:

```bash
cloudflared tunnel login
cloudflared tunnel create jarvis
cloudflared tunnel route dns jarvis jarvis.example.com   # your PUBLIC_HOST
```

That writes credentials under `~/.cloudflared/` and a CNAME in the zone. Put the same
hostname in `.env` as `PUBLIC_HOST`; name the tunnel something else and set
`CLOUDFLARE_TUNNEL` to match. `scripts/dev.sh` and `scripts/install-systemd.sh` run it
for you from there.

### Twilio console

1. Buy (or open) a number → **Voice & Fax → A call comes in**:
   `https://<PUBLIC_HOST>/twilio/voice`, method **HTTP POST**.
2. **Call status changes**: `https://<PUBLIC_HOST>/twilio/status`, method **HTTP POST**.
3. The tunnel hostname is a DNS record you own, so the webhook URL never changes.

Both webhooks are validated against the Twilio signature; `/twilio/voice` additionally
refuses callers outside `ALLOWED_CALLERS`, and the media-stream socket needs a one-time
token minted by `/twilio/voice` for that very call, so a stray connection to the tunnel
gets nothing.

### Google (Gmail and Calendar)

Nothing to set up: the Claude CLI carries your authorized **claude.ai connectors** (Gmail,
Calendar, Drive, and whatever else you have connected), and every subagent inherits them.
Ask for mail or calendar work and it just happens.

The older path — a `workspace-mcp` stdio server of our own — is still in the tree but off
(`GOOGLE_WORKSPACE_MCP=false`). Turn it on only if your subagents authenticate with an
`ANTHROPIC_API_KEY` rather than the subscription login, since the connectors come with that
login. The setup below is for that case.

<details>
<summary>Setting up workspace-mcp (only with GOOGLE_WORKSPACE_MCP=true)</summary>


1. Google Cloud console → **APIs & Services → Credentials → Create credentials → OAuth
   client ID**, type **Desktop app**. Enable the Gmail and Calendar APIs.
2. Put the client id/secret in `.env` as `GOOGLE_OAUTH_CLIENT_ID` /
   `GOOGLE_OAUTH_CLIENT_SECRET` (and your address as `USER_GOOGLE_EMAIL`).
3. Run the one-off consent flow:

   ```bash
   uv run jarvis setup-google
   ```

   It starts `uvx workspace-mcp --tools gmail calendar --transport stdio --single-user`
   once and calls a harmless tool, which opens the browser sign-in. Credentials are stored
   under `~/.jarvis/google/` and reused by every subagent afterwards. `jarvis doctor`
   warns when that directory is still empty.

</details>

## Running

```bash
uv run jarvis serve                  # phone server + "hey jarvis" listener
uv run jarvis serve --no-phone       # local wake word only
uv run jarvis serve --no-wakeword    # phone only (no mic needed)
uv run jarvis serve --fake-agents    # scripted subagents: no Claude tokens spent
uv run jarvis serve --host 0.0.0.0 --port 9000   # override HOST/PORT from .env
```

For the phone channel you also need the tunnel. `scripts/dev.sh` runs both — it starts
the Cloudflare tunnel (logging to `.cloudflared.log`) and `jarvis serve --no-wakeword`,
passing any extra flags through:

```bash
scripts/dev.sh
```

### As a background service (Linux, systemd)

```bash
scripts/install-systemd.sh              # render + start both units
scripts/install-systemd.sh --uninstall  # stop + remove them
```

This renders `ops/systemd/jarvis.service` (runs `uv run --project <repo> jarvis serve
--no-wakeword`) and `ops/systemd/cloudflared.service` (the tunnel to `localhost:$PORT`)
into `~/.config/systemd/user/`, enables both, and turns on lingering with `loginctl
enable-linger` so they keep running with nobody logged in and come back after a reboot.
Both restart on failure. Unit stdout/stderr go to
`~/.jarvis/logs/{jarvis,cloudflared}.{out,err}.log` (and `journalctl --user -u jarvis`);
Jarvis's own log is `~/.jarvis/logs/jarvis.log` (10 MB × 5 rotated files).

The installer stops early if the named tunnel does not exist yet and prints the three
commands above that create it.

### As a background service (macOS, launchd)

```bash
scripts/install-launchd.sh              # render + load both agents
scripts/install-launchd.sh --uninstall  # unload + remove them
```

This renders `ops/launchd/com.william.jarvis.plist` (runs `uv run --project <repo> jarvis
serve`) and `ops/launchd/com.william.ngrok.plist` (a tunnel on `PUBLIC_HOST`) into
`~/Library/LaunchAgents/` and hands them to `launchctl bootstrap`. Both have `RunAtLoad`
and `KeepAlive`, so they start at login and restart if they die. launchd's own stdout/
stderr go to `~/.jarvis/logs/{jarvis,ngrok}.{out,err}.log`; Jarvis's own log is
`~/.jarvis/logs/jarvis.log` (10 MB × 5 rotated files). A Mac that only listens for the
wake word wants `serve --no-phone` and no tunnel agent at all.

Grant the terminal (and, once installed, the launchd agent) **microphone** permission in
System Settings → Privacy & Security, or the wake word never hears anything.

### Restarting it

```bash
uv run jarvis restart --reason "picked up new code"
uv run jarvis restart --status     # how the last one went
```

This asks the service manager (systemd on Linux, launchd on macOS — `SERVICE_MANAGER`
overrides the guess) to restart the unit, and **Jarvis phones you back by itself once it is
up again**, with a one-line status: how long it was down, the version before and after,
which channels are listening, and how many tasks the restart interrupted. You can also just
ask it on the phone — "restart yourself" — and the voice model's `restart_service` does the
same thing; that restart waits until you have hung up, because a restart drops every call in
progress.

Nothing rings while you are already talking to Jarvis: a confirmation that lands during a
call is spoken into that call, and one that cannot be spoken is texted instead. If the call
cannot be placed at all — Twilio down, no `PUBLIC_HOST`, the phone channel not running — the
summary goes out as a text, and if even that fails the attempt is left on the record for
`jarvis restart --status` to read back. Nothing supervising the process (a `jarvis serve` you
started in a terminal) means `restart` refuses: stopping would leave nothing to start it
again.

The one thing it cannot tell you is that the service never came back — nothing of Jarvis's
is left running to notice. That is what `Restart=always` / `KeepAlive` is for; if the call
never comes, `systemctl --user status jarvis` and `~/.jarvis/logs/jarvis.log` are the place
to look.

## Using it

Call the number, or say **"hey jarvis"** at the Mac. Then talk normally:

- *"What's on my plate today?"* — answered directly.
- *"What's the dollar-euro rate?"* — answered on the spot with `web_search`, no task.
- *"Look into how Twilio handles media stream reconnects and summarise it."* — a task.
- *"In the jarvis project, add a retry to the report fetch and run the tests."* — a task
  (PIN required on the phone, as every task is).
- *"Check my mail for the papers the group sent, summarise each one, and tell me."* — one
  task: the same subagent reads the mailbox, uses the `ingest-paper` skill and fans out
  subagents of its own.
- *"What's running?"* / *"How did task 3 go?"* — task status and results.
- *"Add to task 3: also update the README."* — a follow-up into the same subagent.
- *"Call me back when it's done."* — an outbound call when the task lands.
- *"Send me that on Slack."* — the message arrives in your DM; a file or plot is sent by
  the subagent that made it. Only asking gets you one: Jarvis never sends unprompted.
- *"What am I spending this month?"* — read straight off the provider's billing API with
  `check_billing`; see **Asking what it costs** below.
- *"What's free on Alpha?"* / *"Am I still running on Beta?"* — read straight off Slurm
  with `cluster_stats`; see **Asking what the clusters are doing** below.
- *"Goodbye."* — ends the session (locally it also ends after 30 s of silence).

**Anything that is work goes straight to Claude.** Jarvis does not repeat the request back
for a yes, ask which file you mean, or argue about the approach — it dispatches and tells
you it has. If you did not name a project the task starts in `PROJECTS_ROOT` and the
subagent finds the repo itself; the voice prompt already knows every project name there,
so "in the splatting repo" is enough. It also knows every skill installed under
`SKILLS_DIR`, so work a skill covers — a sweep, a profile, a cluster job — is recognised
without you naming the skill.

The questions you get asked are the ones Claude worked out, not the ones the voice model
imagined. A subagent that hits a decision only you can make does everything else first,
then ends with one spoken question; Jarvis asks it and sends your answer back into the
same session as a follow-up.

Short tasks answer inline; longer ones come back as an announcement in whatever session is
live, an SMS with a link to the written report, and a call back if you asked for one.

**The PIN.** On the phone, dispatching anything is refused until you authorize: say the
PIN or key it in on the keypad. Keyed digits are collected in the session and never enter
the model transcript. Three failures ends the call. Local sessions are pre-authorized —
you are already at the machine.

### From the terminal

```bash
uv run jarvis tasks list                      # 20 most recent, newest first
uv run jarvis tasks list --status running     # queued|running|done|failed|cancelled|all
uv run jarvis tasks list --limit 50
uv run jarvis tasks show 12                   # every field, plus the written report
uv run jarvis doctor                          # is this machine set up?
uv run jarvis loopback --wav sample.wav       # one session from a WAV, no mic needed
uv run jarvis restart --reason "new code"     # restart the service; it calls you back
uv run jarvis restart --status                # how the last restart went
```

`jarvis tasks` reads the SQLite store directly, so it works while the server is running
(or when it is not).

## Security model

- **The subagents run as you.** They use `permission_mode="bypassPermissions"`, so a
  `coding` task has your full user access to files, repos, and the network. Treat "who can
  reach Jarvis" as "who can run commands on this machine".
- **What the tunnel exposes** is only: `/twilio/voice` (Twilio signature-validated *and*
  caller-allowlisted), `/twilio/status` (signature-validated only — it carries no caller
  to check), `/twilio/media` (needs a one-time, 60-second stream token minted by
  `/twilio/voice` for the same call), `/reports/{id}?t=…` (HMAC-signed link, the one you
  get by SMS), and `/health`, which is unauthenticated and answers with `ok` plus the
  number of live sessions. Nothing else is served.
- **Caller ID is spoofable**, so the allowlist alone is not a gate. The PIN is what
  actually protects destructive kinds on the phone. Set a long one, keep
  `ALLOWED_CALLERS` tight, and leave `JARVIS_PIN` set — with no PIN configured, `coding`
  and `cowork` are simply refused over the phone.
- Secrets are never logged, the PIN is compared with `hmac.compare_digest`, and the report
  signing secret is either `REPORT_SECRET` or a random one persisted at
  `~/.jarvis/report_secret` with mode 600.

## Costs

- **Realtime voice** is the running cost of a call: roughly **$0.06–0.11 per minute** of
  conversation, audio in and out. A five-minute call is well under a dollar; leaving the
  wake-word listener on costs nothing until a session actually opens.
- **Subagents** run on your Claude subscription by default (they count against the plan's
  usage limits, not per-token billing); with `ANTHROPIC_API_KEY` set they are pay-per-token
  instead. `claude-opus-5` is the default; ask for `sonnet` or `haiku` out loud for
  cheaper work.
- **Ask it what it is spending** — *"what's the bill this month?"* reads the real figure
  off the provider's billing API. See [Asking what it costs](#asking-what-it-costs).
- Guardrails that keep a bad day from becoming an expensive one:

  | Setting | Default | What it caps |
  |---|---|---|
  | `MAX_CALL_SECONDS` | 1800 | a phone call's length — Jarvis is told to wrap up 30 s before, then hangs up |
  | `SUBAGENT_MAX_TURNS` | 200 | agent turns in one task |
  | `SUBAGENT_MAX_BUDGET_USD` | 10.0 | dollars one task may spend |
  | `DAILY_TASK_CAP` | 50 | tasks dispatched per day |
  | `MAX_CONCURRENT_TASKS` | 3 | subagents running at once; the rest queue |

### Asking what it costs

*"What's the bill this month?"*, *"how much has this cost me?"*, *"what has Claude spent?"*
— the voice model answers these itself with the **`check_billing`** tool rather than
dispatching a task. It is read-only: two `GET`s against the provider's billing API and
nothing else. It is deliberately **not** PIN-gated — asking what a number is changes
nothing — and it never puts a key, or any part of one, in its answer or in the log.

**It needs an admin credential.** The key the voice agent talks to the model with cannot
read billing: OpenAI's `/v1/organization/costs` wants an Admin key from
[the org's admin-keys page](https://platform.openai.com/settings/organization/admin-keys),
and Anthropic's cost report wants an `sk-ant-admin…` key. Set `OPENAI_ADMIN_KEY` (and/or
`ANTHROPIC_ADMIN_KEY`). Left unset it falls back to the ordinary key and reports the 401
it gets, which is a clearer answer than a tool that silently is not there.

**Which provider.** `BILLING_PROVIDER` is `auto`, which means **OpenAI** — the account the
call you are on is running against. Say *"what has Claude cost"* and the model passes
`provider: anthropic` for the subagent side. `openai` and `anthropic` pin it either way.

**What comes back** (as a tool result, for the model to speak — never read out verbatim):

```json
{
  "status": "ok",
  "provider": "openai",
  "scope": "organization",
  "currency": "USD",
  "spend_to_date": 31.4021,
  "projected_month_end": 96.14,
  "estimate": true,
  "period_start": "2026-08-01T00:00:00+00:00",
  "period_end":   "2026-09-01T00:00:00+00:00",
  "as_of":        "2026-08-11T09:14:03+00:00",
  "usage": {"input_tokens": 4.1e6, "output_tokens": 310000, "input_audio_tokens": 2.2e6,
            "output_audio_tokens": 1.4e6, "input_cached_tokens": 900000, "requests": 812},
  "top_line_items": [{"name": "gpt-realtime-2.1, input", "amount": 19.8}],
  "spoken": "OpenAI so far this month: 31.40 USD, on track for about 96 by month end."
}
```

- The period is the **UTC calendar month**, because that is how both providers bill.
- `projected_month_end` is a straight-line run rate computed here, not from the provider.
  The prompt makes the model call it an estimate out loud. The elapsed window is floored at
  one day, so asking at half past midnight on the 1st does not project a dollar into two
  and a half thousand.
- `scope` matters: OpenAI's costs endpoint has no per-API-key filter, so the figure is the
  organization's (or one project's, with `OPENAI_BILLING_PROJECT_ID`). Token *usage* can be
  narrowed to a single key with `OPENAI_BILLING_API_KEY_ID`; spend cannot.
- Set `BILLING_MONTHLY_BUDGET` and the answer gains `monthly_budget` and
  `budget_used_percent`, and the spoken line gains "…which is 31 percent of the budget".
  Neither provider serves a spend limit over the API, so that number is yours or nothing.
- A usage-endpoint outage does not lose the spend: `usage` comes back `{}` and the money
  figure still lands.

**When it fails** the tool returns a `status` and a `message` written to be spoken, never
an exception and never a credential:

| `status` | When | What the model is told to say |
|---|---|---|
| `not_configured` | no key for that provider | billing is not set up; offer to have Claude wire it up |
| `auth` | 401/403 | the credential was refused — it needs an *admin* key |
| `rate_limited` | 429, after one retry | rate-limited; offer to try again in a minute |
| `unavailable` | 5xx, timeout, DNS | did not answer; offer to try again |

Rate limits and 5xx are retried **once** after a second; auth failures are not retried.
Logs carry the key as `sk-admin…CRET` — first eight characters and last four — and error
details are reduced to the status code, because a provider's 401 body can quote the key
back at you.

### Asking what the clusters are doing

*"What's free on Alpha?"*, *"Am I still running on Beta?"*, *"How busy is the cluster?"*,
*"How long has my job got left?"* — the voice model answers these itself with the
**`cluster_stats`** tool rather than dispatching a task. Naming no cluster gets you both.

It is read-only: three Slurm reads (`squeue` for your jobs, `sinfo -N` for the partition,
`squeue -t PD` for the queue) batched into one round trip per cluster, with both clusters
asked at once. Nothing it can do submits, cancels or changes a job — that is still work for
Claude, and still PIN-gated. Like `check_billing`, the tool itself needs no PIN: it changes
nothing, and the payload is counts plus your own job ids, never a job name or a path.

**How it reaches the cluster.** Every command goes through the `cluster-compute` skill's ssh
guard — `CLUSTER_SSH_GUARD`, by default
`~/.claude/skills/cluster-compute/scripts/cluster_ssh.sh`. That is not swappable for a plain
connection: cluster auth is Duo 2FA behind an ssh ControlMaster, a non-interactive process
cannot answer a Duo push, and an attempt against a dead master hangs rather than failing —
a retry storm of those once got this machine's IP fail2ban-banned. The guard probes the
*local* control socket first, so a dead session costs nothing and produces no failed login.
Jarvis never retries it, and never opens a connection of its own.

Which clusters: **beta** (partition `pci`) and **alpha** (partition `gpu`). Delta is
deliberately absent — a third Duo session nobody keeps alive would answer every question
with "expired".

The free-GPU count already excludes GPUs that are `down` and GPUs that backfill has
`planned` for a queued job, and those are reported separately rather than folded in; the
queue count separates jobs actually waiting for hardware from ones blocked on a dependency.
Both distinctions matter: a partition can look like it has 22 GPUs free and have none.

**When it fails** the tool returns a `status` and a `message` written to be spoken, per
cluster — one cluster being unreachable never costs you the other:

| `status` | When | What the model is told to say |
|---|---|---|
| `auth_expired` | the Duo/ControlMaster session timed out | he needs to approve a Duo push on the desktop first |
| `not_configured` | no ssh guard on this machine | cluster access is not set up; offer to have Claude wire it up |
| `unknown_cluster` | a cluster it does not know | it knows Beta and Alpha; ask which he meant |
| `timeout` | no answer within `CLUSTER_QUERY_TIMEOUT_S` (20 s) | offer to try again in a moment |
| `unavailable` | the guard or Slurm failed | the numbers are not available; offer to put Claude on it |

To clear an `auth_expired`, re-open the ControlMaster on this machine the way the
`cluster-compute` skill documents (a backgrounded, no-command login to `beta` or `alpha`)
and approve the Duo push; the next call answers normally.

## Troubleshooting

Start with:

```bash
uv run jarvis doctor        # add --no-mic on a machine with no microphone
```

It checks `.env`, both API keys, the `claude` CLI (bundled or on `PATH`), Twilio
settings, `PUBLIC_HOST`, a tunnel binary (`cloudflared` or `ngrok`), the caller allowlist,
the PIN, the wake-word model, the
microphone, that `~/.jarvis` is writable, and whether Google credentials exist. `✅` is
fine, `⚠️` narrows what Jarvis can do (no mic, no PIN, no Google, no `claude` CLI), `❌`
means it will not work — and only `❌` makes the command exit non-zero.

Where to look when something misbehaves:

| Thing | Where |
|---|---|
| Server log | `~/.jarvis/logs/jarvis.log` (rotated, 10 MB × 5) |
| Service stdout/stderr | `~/.jarvis/logs/{jarvis,cloudflared,ngrok}.{out,err}.log` |
| Voice transcripts | `~/.jarvis/calls/<session_id>.log` |
| Subagent progress | `~/.jarvis/tasks/<id>.log` |
| Written reports | `~/.jarvis/tasks/<id>.md` |
| Task rows | `~/.jarvis/tasks.db` (or just `jarvis tasks show <id>`) |

Common cases:

- **The call connects but nobody speaks** — the media socket was rejected. Check the
  caller is in `ALLOWED_CALLERS` and that the webhook URL matches `PUBLIC_HOST` exactly.
- **Twilio shows 403** — signature validation failed; the tunnel host and the configured
  webhook URL disagree.
- **"hey jarvis" does nothing** — run `jarvis download-models`, check microphone
  permission, and try lowering `WAKEWORD_THRESHOLD`. On Linux there is no wake word at
  all: openwakeword is not installable there, and `doctor` says so.
- **Tasks fail instantly** — no subagent auth (subscription login, token, or API key) or
  the `claude` CLI is missing (`jarvis doctor` says so).
- **Work is refused on the phone** — no PIN configured, or you have not entered it yet.
- **Google tools fail in a task** — run `jarvis setup-google` again; the stored
  credentials may have expired.

## Development

```bash
uv run pytest -q               # must be green with no warnings
uv run ruff check src tests
uv run jarvis serve --fake-agents   # scripted subagents, no Claude tokens
uv run jarvis loopback --wav sample.wav --out reply.wav
```

Tests never touch the network, a microphone, or a real subagent: OpenAI, Twilio,
sounddevice, openWakeWord, and the Agent SDK all sit behind injectable interfaces with
fakes.

## Licence

[Apache License 2.0](LICENSE) — Copyright 2026 William Koch.

Apache-2.0 rather than MIT for two things MIT does not give you: an explicit patent
grant from every contributor, and a trademark clause, which matters because "Jarvis" is a
name with prior art all over it. Use the code; the name is not part of the grant.

No third-party code is vendored into this repository. Everything else arrives through
`pyproject.toml` under its own licence.
