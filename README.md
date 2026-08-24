# Jarvis

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

Tools the model can call: `dispatch_task`, `list_tasks`, `get_task_status`,
`get_task_result`, `send_followup`, `cancel_task`, `list_projects`, `request_callback`,
`submit_pin`, `end_session`.

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
- A **Google Cloud OAuth client** (Gmail + Calendar scopes) if you want `cowork` tasks

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

Useful optional ones: `HOST`/`PORT` (default `127.0.0.1:8080`), `DATA_DIR` (default
`~/.jarvis`), `PROJECTS_ROOT` (every subdirectory is dispatchable by name), `SKILLS_DIR`
(default `~/.claude/skills`, listed in the voice prompt), `SUBAGENT_MODEL` (default
`claude-opus-5`), `LOG_LEVEL`, and the guardrails below. The full table is spec §3.4.

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

### Google (optional, for `cowork` tasks)

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
   under `~/.jarvis/google/` and reused by every `cowork` subagent afterwards. `jarvis
   doctor` warns when that directory is still empty.

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

## Using it

Call the number, or say **"hey jarvis"** at the Mac. Then talk normally:

- *"What's on my plate today?"* — chat, answered directly.
- *"Look into how Twilio handles media stream reconnects and summarise it."* — a
  `research` task.
- *"In the jarvis project, add a retry to the report fetch and run the tests."* — a
  `coding` task (PIN required on the phone).
- *"Check my mail for anything from the accountant and put a slot in my calendar."* — a
  `cowork` task (PIN required on the phone).
- *"What's running?"* / *"How did task 3 go?"* — task status and results.
- *"Add to task 3: also update the README."* — a follow-up into the same subagent.
- *"Call me back when it's done."* — an outbound call when the task lands.
- *"Goodbye."* — ends the session (locally it also ends after 30 s of silence).

Task kinds and what each subagent may touch: `chat` (read-only tools), `research` (adds
`Write`), `coding` (everything, in the project's checkout), `cowork` (read-only plus Gmail
and Calendar).

**Anything code-shaped goes straight to Claude.** Jarvis does not repeat the request back
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

**The PIN.** On the phone, `coding` and `cowork` are refused until you authorize: say the
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
- Guardrails that keep a bad day from becoming an expensive one:

  | Setting | Default | What it caps |
  |---|---|---|
  | `MAX_CALL_SECONDS` | 1800 | a phone call's length — Jarvis is told to wrap up 30 s before, then hangs up |
  | `SUBAGENT_MAX_TURNS` | 200 | agent turns in one task |
  | `SUBAGENT_MAX_BUDGET_USD` | 10.0 | dollars one task may spend |
  | `DAILY_TASK_CAP` | 50 | tasks dispatched per day |
  | `MAX_CONCURRENT_TASKS` | 3 | subagents running at once; the rest queue |

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
- **`coding` is refused on the phone** — no PIN configured, or you have not entered it yet.
- **Google tools fail in a `cowork` task** — run `jarvis setup-google` again; the stored
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
