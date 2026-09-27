# CLAUDE.md

Jarvis: a Twilio phone + local wake-word voice agent, backed by the OpenAI
Realtime API and Claude Agent SDK subagents.

## Commands

- Install: `uv sync` (every coding agent, via the default `agents` group); one agent only:
  `uv sync --no-group agents --extra codex` (or `claude`); with pip, `jarvis[all|claude|codex]`
- Run tests: `uv run pytest -q` (coverage: `uv run pytest -q --cov`, floor 96%)
- Lint: `uv run ruff check src tests`
- Run the CLI: `uv run jarvis --help`
- Check the machine's setup: `uv run jarvis doctor` (`--no-mic` where there is none)
- Choose and sign in the coding agent: `uv run jarvis setup-agent [--default claude|codex]
  [--enable NAME]… [--no-smoke] [--yes] [--json]` (runs the missing login on the terminal,
  smoke-tests each chosen agent with one real task, prints `AGENT_BACKEND=` /
  `AGENTS_ENABLED=`; never reads or writes `.env`). `--json` needs `--yes`; exit 1 is a
  chosen agent that cannot run a task, 2 a wrong command line
- Run the agent: `uv run jarvis serve` (`--no-phone` / `--no-wakeword` /
  `--fake-agents` / `--host` / `--port`); `scripts/dev.sh` adds the Cloudflare tunnel
- Approval bridge: `uv run jarvis approvals` (`--limit N`, `--disable` / `--enable` for
  the kill switch); install the Claude hook with `scripts/install-claude-hook.sh`
- Inspect tasks: `uv run jarvis tasks list [--status …] [--limit N] [--internal]`,
  `uv run jarvis tasks show <id>` (the `TOLD` column is `NO` until Jarvis has said it)
- Read what Jarvis remembers between calls: `uv run jarvis memory` (`--path` for the file)
- Start that memory before any call has: `uv run jarvis init [--name NAME] [--fact TEXT]…
  [--from FILE|-] [--force] [--yes] [--json]` (writes `memory.md` only, never `.env`; prints
  the `OWNER_NAME=` line and what every call will carry). `--json` is the same report for an
  agent, needs `--yes`, and exits 1 only when a memory was wanted and not written, 2 on a
  wrong command line; `skills/jarvis-onboard` is the keyboard session that drives it
- Delete transcripts and finished task rows: `uv run jarvis forget [--older-than N]`
  (`--transcripts-only` / `--tasks-only` / `--yes`)
- Restart the service: `uv run jarvis restart [--reason …] [--force] [--no-callback]`
  (it phones back when it is up again, and rings with a plain spoken alert — texting too,
  when `SMS_ENABLED` is on — if it never comes back);
  `uv run jarvis restart --status` for the last one, including what the logs said
- One-off setup: `uv run jarvis download-models`, `uv run jarvis setup-google`,
  `uv run jarvis setup-gmail` (read-only sign-in for `check_email`; prints a link, then
  `--finish '<redirect URL>'`, so it works with no browser on the machine)
- Background service: `scripts/install-systemd.sh [--uninstall]` on Linux,
  `scripts/install-launchd.sh [--uninstall]` on macOS

## Layout

Source lives under `src/jarvis/` (installable package, `src/` layout). Tests
live under `tests/`, mirroring the package structure. `cli.py` stays argument
parsing plus wiring: the `doctor` checks live in `jarvis/doctor.py`, the `init` flow in
`jarvis/onboarding.py` and the Google OAuth bootstrap in `jarvis/google_setup.py`. Service
templates are in `ops/systemd/` (Linux) and `ops/launchd/` (macOS), rendered by the matching
`scripts/install-*.sh`; `scripts/lib.sh` holds the scaffolding those scripts share
(argument parsing, the env-file and PATH checks, `render`), so an installer is only
its platform-specific half.

Six groups are named here because the file you want is rarely the one whose name you
remember:

- **agents** — `agents/` is one module per coding agent (`claude`, `codex`) behind the
  `AgentRunner` in `agents/base.py`, which also holds `RunResult`, `TokenUsage`, the
  `SPOKEN_SUMMARY:` / `RESTART_REQUIRED:` parsing and the fake. `agents/session.py` is the one
  session every agent runs through: a backend is an *adapter* (its client's messages as
  `Text`/`ToolCall`/`FileEdit`/`SessionId`/`Notice`/`Done`) plus an `AdapterRunner` that
  `connect()`s one, and everything else — progress, summary, restart request, usage,
  redaction — is written once there. `agents/registry.py::BACKENDS` is the table everything
  else reads (a third agent is an adapter and a connector in one module, one entry, its
  `AgentName`, its settings and a `docs/agents.md` column), `agents/auth.py` the three auth
  tiers both share, `agents/router.py` opens each task on `task.agent`.
  `tasks/agent_runner.py` only re-exports the spec §3.2 names. `docs/agents.md` is the parity
  matrix, and `tests/test_docs_sync.py` wants a column in it for every backend.

- **restart** — `restart/` is the whole subsystem: `coordinator`, `service`, `store`,
  `version`, `watchdog` and `logscan`. The directory listing is the index now.
- **tools** — `tools/builtin.py` is a composition root; the registrations are in
  `builtin_comms`, `builtin_billing`, `builtin_tasks`, `builtin_restart` and
  `builtin_session`, with the wording, the parsing and the gates (`pin_gate`, `read_gate`,
  `possession_gate`, `get_task`) in `builtin_common`. **The order `builtin.py` calls them in is the order
  the tools are offered to the model.** A new tool goes in a domain module and the
  `docs/tools.md` table, or `tests/test_docs_sync.py` fails. A tool may also be registered `silent=True`
  (`mark_reported`, `end_session`): its result is submitted without asking for a response,
  because both are called *after* the thing worth saying has been said and the turn would
  only be spent saying it again. Only for those; anything the owner is waiting to hear keeps
  its turn.
- **notify** — `notify/deliver.py` holds `announce_to_live_sessions` and `safe_send_sms`.
  The `can_text` gate is asserted there and nowhere else.
- **integrations** — `integrations/` is one module per outside service (`billing`,
  `cluster`, `gmail`, `slack`, `web_search`), each behind exactly one voice tool. The tool's
  *registration* goes in `tools/builtin_<domain>.py`; its *client* goes here.
- **continuity** — `continuity/` is what survives the end of a call: `briefing`, `memory`
  and `recall` (the three pieces below), plus `transcripts`, the call log they read, and
  `retention`, which prunes exactly those artefacts.

`logging_util.mask_number` is the only shape a phone number may take in a log line.

## One task kind

There is one `TaskKind` (`agent`) and no per-kind tool restriction: every subagent gets
the full built-in tool set, the Google MCP server, the installed skills and subagents of
its own, and decides for itself what a request needs. The voice model's only routing
decision is answer-it-myself (small facts go through its `web_search` tool, backed by the
Responses API) versus dispatch. Do not reintroduce kinds to express "this one is
read-only" — the phone PIN gates every dispatch instead.

## Two coding agents, one of them per task

`AGENT_BACKEND` (Claude or Codex) runs what nobody named an agent for; `AGENTS_ENABLED` is
what the voice may name. Five rulings:

- **A task keeps its agent for life.** `Task.agent` is fixed at dispatch, and every resume
  opens on it, never on today's default: a session id belongs to the agent that issued it.
  The column `claude_session_id` holds whichever agent's id, and keeps its name for the
  build running behind the database.
- **A credential goes in the child's environment and nowhere else** — not argv, not a log,
  not a spoken error (`agents/auth.redact`, and `AdapterSession` redacts every error it
  returns or logs of the credential *and* of every MCP secret the agent was handed). One
  exception, forced by Codex's app-server ignoring `CODEX_API_KEY`: that key is logged in
  once, on stdin, into `data_dir/codex`, never the owner's `~/.codex`. `OPENAI_API_KEY` is
  never lent to Codex: that would move a ChatGPT-plan user onto per-token billing unasked,
  and because the SDK copies Jarvis's environment into the app-server, every credential
  variable the chosen tier does not use is overridden with an empty value.
- **Both agents run on their vendor's SDK, whose client is injectable.** Claude on the Agent
  SDK (driving it as `claude -p` would lose `max_budget_usd`, `max_turns`, the lifted
  message-size limit and the typed messages); Codex on `openai-codex`, pinned exactly because
  the adapter reads its generated types field by field, through the `CodexClient` protocol.
  `SUBAGENT_TIMEOUT_S` is the cap they share, applied in the task manager, which interrupts
  and closes a timed-out session before it records anything.
- **A follow-up goes into a running turn only where that is safe.** Codex steers it in;
  Claude's `send()` refuses (`SteerUnavailable`), because a `query()` after its final text
  starts a turn nobody reads, and the manager re-runs instead. Only a refusal is re-queued:
  any other steer failure may have landed. The per-task lock in the manager serializes
  taking a follow-up in with closing the row out; keep it.
- **Prompts say "Claude", and code names the agent.** Templates are live under whatever build
  is running, which blanks a placeholder it does not know, so the voice prompt's own wording
  is rewritten to the default agent's name in `prompts._name_the_agent` rather than templated.
  The one new placeholder, `{agents}`, is a paragraph of its own that blanks cleanly.

## Trust has three levels

Caller ID is spoofable, so an inbound number proves nothing — but a call *Jarvis placed* is
different in kind, and one bit of trust could not say so. `jarvis/trust.py` has the three,
ordered so everything asks for "at least this much":

- **`NONE`** — an inbound call before the PIN.
- **`POSSESSION`** — a call Jarvis placed to `Settings.owner_number`. Reaching that phone
  means holding it.
- **`FULL`** — the PIN was given on this call, or the channel is the local microphone.
  `VoiceSession.trusted` is the old spelling of exactly this, and still means it.

Four rulings, and `SECURITY.md` is the threat model:

- **A token is the only thing that may confer possession.** `stream_tokens.outbound_extra`
  records that Jarvis placed the call and the number it dialled; `confers_possession` applies
  the rule in `_open_session`, against `Settings.owner_numbers` — never Twilio's `From`/`To`,
  which are the caller's carrier talking. That set is the whole allowlist, because Jarvis has
  one owner: `ALLOWED_CALLERS` is the handsets one person picks up, not a guest list, and
  `OWNER_NUMBER` only chooses which one Jarvis rings first. Do not narrow it back to the one
  number — that only makes the tier fail silently on the owner's other phone.
- **The PIN is the line between reading and acting, not between private and not.** The
  owner's ruling, and the reasoning is why it is written down: the threat case is somebody
  who has the machine, and they have `.env`, which has `JARVIS_PIN` — so gating reads buys
  nothing against them. It only ever defended against a phone-side caller-id spoofer, and it
  charged that defence to every ordinary call. So the whole standing briefing comes before
  the PIN (`BRIEFING_BEFORE_PIN`, default on): the digest, the memory, the project names, the
  briefs, the skills. `false` restores the older silence exactly, and the `withheld`
  machinery in `prompts/__init__.py` exists for that — do not delete it. `announce(text,
  needs=…)` says which kind each announcement is: news needs nothing, an approval needs a
  call that could answer it. `Announced.delivered` still takes `POSSESSION`, because a
  stranger hearing the news is not the owner having been told, so the text and the call-back
  still go out.
- **The read-only tools over that same material follow it.** `list_projects`, `list_tasks`,
  `get_task_status` and `get_task_result` are the caller asking for a bit of the briefing out
  loud, and gating them while the prompt states the same facts is incoherent; `read_gate` is
  the gate, and it follows `BRIEFING_BEFORE_PIN` so that setting has no hole in it.
  **`recall` is not one of them and stays at `FULL`** — the briefing is a bounded, curated
  context the owner can read with `jarvis memory` and prune, and it is the same whatever the
  caller says, where `recall` is an unbounded, caller-steered query over every raw transcript
  Jarvis has ever written. That is a different quantity of exposure, and the one thing on the
  phone a spoofer could actually mine.
- **Possession is who is holding the phone, not that they meant to spend the machine.** It
  buys `send_followup` and `request_callback` (the answer to the question Claude came back
  with — the point of the tier), the two approval tools, and `mark_reported` on anything.
  Dispatch, `recall`, `send_to_slack` and `restart_service` still need the PIN.
  `possession_gate` is the gate; `pin_gate` is unchanged and still means `FULL`.
- **Voicemail must not be able to act.** An outbound call can be answered by an answering
  machine, which will listen to a result and say something machine-shaped back. Listening is
  unchanged; *acting* on speech at `POSSESSION` wants one DTMF press earlier in the same call
  (`VoiceSession.keypressed`). A keypad approval is already a press and asks for nothing more.
  Two things can want that keypad, so `Keypad.armed` hands it a digit only while a menu is
  actually waiting on one, and `PIN_ENTRY_KEY` (`*`, which is neither part of a PIN nor a menu
  option) toggles it back to the PIN when one is. Both halves are needed: the first for the
  call with no menu up, the second for the call with one. A spoken PIN was always the third
  way through — `submit_pin` is ungated at every level.

What has not moved, and there is a test named after each: `mark_reported` below `POSSESSION`
may stamp only `reportable_task_ids` — the tasks this call's own digest named, plus
`opening_task_id` — because stamping decides what the owner never hears.
**`SessionEnded.authorized` is still `FULL` only**, so a call that never gave the PIN reads
the memory and never rewrites it, and no `recall` runs over its transcript. That is the
single most important invariant here: hearing what Jarvis believes is recoverable, editing it
is not. A spoken PIN is `[PIN]` in every transcript line
(`continuity.transcripts.redact_pin`), and everything that hands a transcript to a model
redacts again, for logs written before. Do not narrow the subagent's tools instead.

## Continuity is three pieces

A realtime session starts blank — the provider keeps nothing across sockets — so what
Jarvis knows at the top of a call is assembled every time by
`jarvis/continuity/briefing.py`:

- **The digest.** `Task.reported_at` is the only record that Jarvis *told the owner*; `announced`
  and `sms_sent` only say a delivery was attempted, and neither survives a call they missed.
  Until `reported_at` is stamped, the task rides at the top of the next call — from the
  greeting, PIN or no PIN, along with the rest of the standing briefing
  (`BRIEFING_BEFORE_PIN`; see "Trust has three levels"). Exactly one
  thing stamps it: the voice model's `mark_reported` tool, after it has spoken the result.
  Do not stamp it from a delivery path — hearing something twice is recoverable, never
  hearing it is not.
- **The memory.** `jarvis/continuity/memory.py` owns `data_dir/memory.md` outright — the
  file API *and* the writer. It subscribes to `SessionEnded` and, for an authorized call only,
  dispatches a subagent (`prompts/memory_update.md`) that folds the call into the file; the
  next call reads it back through `briefing`. Its headings are nested one level when
  embedded, so its sections cannot be mistaken for instructions. `memory_skeleton(owner)` is
  the only place its sections are written down — the update prompt renders it, and
  `seed_memory` (behind `jarvis init`) fills it; never restate the structure elsewhere.
  Its *absence* is the marker of a first call: a trusted session with no memory renders
  `prompts/first_call.md` in place of it and opens as a short introduction instead of an
  ordinary call. That is the only record of "has been onboarded" — do not add a second one,
  and do not have the session write `memory.md` itself; the interview's last turn says the
  facts out loud, and the updater folds them in like any other call's.
- **`recall`.** `jarvis/continuity/recall.py` searches past transcripts and past task
  summaries on demand. Matching stays literal on purpose: the query is speech that
  transcription has already mangled once, and a fuzzy hit gets read out as if it were fact.
  It needs the PIN, redacts it, and skips calls that never gave it — the one read that did
  not move when the briefing did, because it is unbounded and the caller steers it.

`Task.internal` marks work Jarvis asked for itself (today: the memory update). It hides the
task from the spoken lists, the digest, `recall`, the daily cap and the notifier — and
restricts *nothing* about the subagent. It is not a task kind; do not grow it into one.

`Task.needs_restart` is the other flag, and it is a *request*, not a fact: the subagent says
`RESTART_REQUIRED: <why>` above its `SPOKEN_SUMMARY:` because it is the only thing that knows
it edited `src/jarvis/**` (`git describe --dirty` flips on any open edit). Honoured only on a
task that succeeded, never on an internal one. The Notifier then hands that task's call-back
to the restart's confirmation, which carries both halves — what the work came to, and whether
it is running. Do not make a subagent restart Jarvis itself; it is inside the cgroup.

## Restarts are three halves

The process that runs `systemctl restart` is the one that gets killed, so
`jarvis/restart/coordinator.py` splits the flow across that death and joins it with
`data_dir/restart.json`: `request()` writes the record and hands over, `resume()` (one task
per `jarvis serve`) finds it on the far side and rings back with a status summary. Neither
half may interrupt a call — a restart asked for during one waits for the line to clear, and
the confirmation is announced or texted rather than dialled into a live session. Keep it
that way, and keep every failure path landing somewhere a human can find it
(`jarvis restart --status`). Whether a restart may be attempted at all is a fact about the
*process*: `SERVICE_MANAGER=auto` resolves from its own cgroup (systemd) or
`XPC_SERVICE_NAME` (launchd), never from `systemctl` being on PATH, so a hand-started copy
refuses rather than restart the installed one. Only `jarvis restart` and `doctor`, which run
outside the unit, ask whether it is installed.

"Did it load the change" is answered from `data_dir/running-version`, stamped by `mark_running()`
at the top of `jarvis serve` — *not* from `current_version()` at request time. The checkout moves
under a running process, and the normal order (edit, commit, ask for the restart) puts the new
commit on disk before the question is put, so a request-time read compares the new commit with
itself and reports that nothing loaded. Process start is the only moment the checkout and the
running code are the same thing.

The third half is `jarvis/restart/watchdog.py`, and it exists because the first two both live
*inside* Jarvis. A restart is usually loading a change Jarvis just made to its own code; a
change that will not import means there is no new process, so nothing runs `resume()` and
nobody is told anything — silence that reads exactly like success. So `_execute()` arms
`jarvis restart-watch` in a transient `systemd-run --user` unit *just before* handing over
(a restart signals the whole cgroup; anything we merely fork dies with us), and it acts only
on the case neither other half can see: a record still `pending` at the deadline. It alerts
by text plus a plain `<Say>` call — never `<Connect><Stream>`, whose media stream is served
by the process that is not running.

Two rulings that look like bugs if you do not know them. A **negative** exit code from the
restart command is the restart working: `systemctl` is inside the cgroup it tears down, so it
is killed handing over and returns `-15`. And "back up" is not "working" —
`jarvis/restart/logscan.py` scopes the service's log files by byte offset (`marks()` before,
`errors_since()` after) so the confirmation can say what broke, and those errors are spoken
*before* the housekeeping.

## The approval bridge runs the other way

Everything else in Jarvis carries a result *outwards* from work the owner asked for.
`jarvis/approvals/` is the opposite: a Claude Code session on their own screen has stopped
and asked *them* something, and they are not at the keyboard. A hook in `~/.claude/hooks/`
(canonical copy: `scripts/claude_hooks/jarvis_approval.py`, installed by
`scripts/install-claude-hook.sh`) hands the pending prompt to the broker over a Unix socket
and blocks; five minutes later, if they still have not answered, Jarvis rings them.

Four rulings hold it up, and none of them is a preference:

- **A Unix socket, never an HTTP route.** `cloudflared` puts the whole of port 8080 on the
  internet. `data_dir/approvals.sock` at 0600 is unreachable through it by construction.
- **`policy.py` is the *primary* control, not a second layer.** A `PermissionRequest` hook
  returning `allow` appears to skip the CLI's own `permissions.deny` re-check, so whatever
  `classify` calls eligible is what a keypad digit can run. It is an allowlist, it starts
  small, and the denylist wins over it. Do not widen it without saying why in the commit.
  It decides on exactly what runs: the raw command's argv (never a normalised copy, never
  a request the hook had to trim), read back whole or not at all.
- **The keypad decides, never the transcription.** `answer_approval` cannot answer
  anything; the most it does is put a menu in the model's mouth. `ApprovalBroker.digit` is
  the only thing in Jarvis that can approve a tool call, it is reachable only from a call
  that has proved something — `FULL`, or the `POSSESSION` of a call Jarvis placed to the
  owner's own number, which is what the escalation call itself is (`VoiceSession._on_dtmf`
  routes there once `authorized`, or while `Keypad.armed`) — and an unrecognised key re-asks
  rather than agreeing. `policy.py`'s allowlist is already the "routine and reversible"
  filter, which is what licenses the second half.
- **Failure is always "do nothing".** Broker down, socket missing, Twilio broken, call
  unanswered, malformed reply, hook crash: all end with the hook printing nothing, which
  leaves the ordinary on-screen prompt exactly as it is. There is no path where an error
  approves something.

Pending is a fact to be re-checked, never assumed: the hook is *not* killed when they answer
at the keyboard, so `PostToolUse`/`PermissionDenied`/`Stop`/`SessionEnd` cancel the
escalation, and pending is re-read before dialling and again before any verdict is applied.
A prompt that arrives while they are already on a call that could answer it is announced into
that call rather than ringing them a second time. `uv run jarvis approvals` is the audit trail and
`--disable` is the kill switch, which is a file so it works without a restart.

## Billing reads, and only reads

`jarvis/integrations/billing.py` answers "what am I spending" from the provider's own billing API,
behind the voice model's `check_billing`. Four rulings, and the first two are the ones
that bite:

- **Anthropic's amounts are decimal strings in cents.** `"123.45"` USD is `$1.2345`.
  Divide by a hundred; there is a test named after it. OpenAI's `amount.value` is a float
  in dollars. The two providers do not agree, and a hundred-fold error read out loud as
  money is the worst thing this feature can do.
- **It needs an admin key, not the agent's key.** `OPENAI_API_KEY` gets a 401 on
  `/v1/organization/costs`; `OPENAI_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY` are separate
  settings. With neither set we still *try* the ordinary key and report the 401, because
  a clear "that needs an admin key" beats a tool that is silently not registered.
- **`GET` and nothing else.** `_get` takes no body and no method, so no caller can turn it
  into a write. Keep it that way, and keep the tool un-PIN-gated: it is the one capability
  in Jarvis that cannot change anything, and asking what a number is should not need a PIN.
- **The spend figure is not per-key, and says so.** OpenAI's costs endpoint filters by
  `project_ids` and nothing finer; `BillingReport.scope` carries what the number actually
  covers. Token *usage* can be narrowed to a key id. Do not let the two blur.

Nothing reaches the caller but the report: `as_dict()` carries no credential, `classify`
reduces a failure to its status code (a 401 body can quote the key back), and the only
place a key appears at all is `redact()` in a log line. Every failure is a `status` plus a
sentence written to be spoken, never a raised exception.

## Cluster stats read, and only read

`jarvis/integrations/cluster.py` answers "what's free on the cluster" and "am I still running"
from Slurm, behind the voice model's `cluster_stats`. It is a worked example, not a default:
the clusters (`CLUSTERS`, `{"name": "partition"}`) and the guard (`CLUSTER_SSH_GUARD`) are
both empty out of the box, and until both are set and the guard is on disk
`build_cluster_stats` returns None, the tool is not registered, and the voice prompt's
`voice_tool_cluster_stats.md` paragraph is not rendered. Never hardcode a cluster. Three
rulings, and the first is the one with a scar behind it:

- **Never our own connection to the cluster.** Where auth is 2FA behind an ssh
  ControlMaster, no non-interactive process can answer the second factor: a direct attempt
  against a dead master *hangs*, and a storm of those retries is how an address gets banned
  by the login nodes. Everything goes through the guard, which probes the local control
  socket — no network, no auth attempt — and exits `42`. That `42` is terminal: nothing
  retries it, and a guard gone missing is `not_configured`, never a fallback that dials out
  by itself. `CLUSTER_SSH_NO_NOTIFY=1` is set because a guard may notify the owner some other way,
  and they are on the phone, which is where the sentence belongs.
- **Read-only by construction.** `build_script` assembles the remote command from module
  constants and refuses any command whose first word is not in `READ_ONLY` (`squeue`,
  `sinfo`); there is a test named after it. The only thing the model chooses is a cluster
  *name*, looked up in the configured set and refused when it is not there — no string
  from the model reaches a shell, and `Settings` refuses a name or partition that is not a
  bare word. Un-PIN-gated for the same reason as `check_billing`, and the payload is counts
  plus the owner's own job ids: no job name, no path, no other user.
- **Idle, planned and down are three numbers, not one.** `sinfo` without `-N` aggregates by
  state line and its totals are silently wrong; a `planned` node is backfill holding
  hardware for a queued job, not a free one; and most pending jobs are blocked on a
  dependency rather than competing for GPUs. The fixtures in
  `tests/integrations/test_cluster.py` are synthetic but preserve the shapes of real cluster
  output, and the totals asserted on are worked out by hand from their rows. Do not collapse
  them to be brief.

## Platforms

macOS runs both channels; Linux runs the phone channel only, because openwakeword
needs `tflite-runtime`, which has no cp312 wheel. `sounddevice`, `openwakeword` and
`onnxruntime` are therefore `sys_platform == 'darwin'` dependencies, and on a Linux host
`jarvis serve` finds them missing (`wakeword_unavailable`, which imports nothing), says so in
one line and serves the phone alone — one more reason every import of them stays lazy.

The coding agents are optional the same way, by choice rather than platform: each SDK is an
extra (`claude`, `codex`, `all`), because each bundles a CLI of hundreds of megabytes. So
`claude_agent_sdk` and `openai_codex` are imported only where an agent runs, never at module
scope, and an agent whose package is missing is `registry.installed() == False`: shown as
not installed with its `uv sync --extra` command, never offered, refused as `AGENT_BACKEND`
by `Settings.agent_refusal`. `tests/agents/conftest.py` skips a backend's own tests without
its SDK, and a test that is not about installation asks for `every_agent_installed`.

## Testing rule

No network or hardware access in tests. OpenAI, Twilio, sounddevice,
openwakeword, the Claude Agent SDK and the Codex SDK are always accessed through an
injectable interface (a `Protocol`) with a fake/test double used in tests —
never the real network or hardware, and never a real `codex` process: the Codex
tests replay `tests/agents/fixtures/codex_app_*.jsonl`, recorded from the bundled
app-server, into the SDK's own typed models. Heavy/hardware imports (`sounddevice`,
`openwakeword`) must be guarded inside functions, not imported at module
scope, so the test suite can run on a machine with no mic.

## Working agreements

- Only scripts read the env file; never print or paste its contents. `.env.example` is a
  different thing — tracked, secret-free, and the one place every setting is listed; keep
  it in step with `Settings` (`tests/test_docs_sync.py` enforces that).
- The database runs ahead of the code. `_migrate` upgrades `tasks.db` from whichever process
  opens it first, and `jarvis serve` holds the `Task` it imported at startup, so a new column
  reaches the file while the service is still a build behind. `Task.from_row` drops columns it
  has no field for; keep it that way, and keep writes naming their columns so the older build
  cannot blank the newer one's data.
- Jarvis does not text. `SMS_ENABLED` is off by default (many accounts lack SMS permission
  for their region, and Slack is the written channel), so gate any send on
  `TwilioOut.can_text` and never on `configured` — outbound *calls* are unaffected, and the
  restart watchdog's `<Say>` alert is the last thing working when Jarvis is down.
- **One action is one sentence.** The wording the model is handed — the tool descriptions,
  the `*_MESSAGE` constants in `builtin_common`, the call-back contexts, the voice prompt —
  says what *not* to say as firmly as what to say, because the failure mode is never
  silence, it is a second turn restating the first. Transcripts of real calls are in
  `~/.jarvis/calls/`; read a few before editing any of it. They may contain a spoken PIN and
  other personal details, so nothing from them is ever copied into code, tests, docs or
  commit messages — describe the pattern, never quote the call.
- Spec §3.2 interface names and signatures stay stable (extra optional keyword
  arguments are fine). §3.3/§4 hold rulings: follow them, and amend the spec in a
  docs commit when one changes.
- Conventional commits (`feat:`/`fix:`/`chore:`/`docs:`) with the Co-Authored-By
  Claude trailer. A subagent Jarvis dispatched adds `Jarvis-Task: <id>` as well, so
  `git log --grep '^Jarvis-Task:'` is everything the owner asked for out loud rather than
  typed — the one thing `git log` cannot otherwise recover.
- Clean and minimal over clever; TDD, with `uv run pytest -q` and
  `uv run ruff check src tests` pristine before a commit. Coverage has a floor (96%) and it
  is a ratchet: raise it when the measured number moves up, never lower it to pass.
- `data_dir` is 0700 and the files under it 0600 (`config.secure_dir` / `secure_file`).
  Anything new that writes there goes through them.
- A configured `JARVIS_PIN` is 6-8 digits and `jarvis serve` refuses to start otherwise.
  Wrong PINs also count across calls (`jarvis/pin_guard.py`): while that has PIN entry locked
  the right PIN is refused before it is compared, and nothing resets the count early — not
  the lock lifting, not a right PIN. That a spoofed caller can keep the owner's PIN locked is the
  accepted price (SECURITY.md); do not buy it back with a reset-on-success or a per-caller
  count, both of which hand a guesser a fresh budget. `serve` also refuses the phone channel
  with `DEBUG_SKIP_TWILIO_VALIDATION` on behind a `PUBLIC_HOST`.

## Reference docs

- Design spec: `docs/superpowers/specs/2026-08-18-jarvis-voice-agent-design.md`
- Implementation plan: `docs/superpowers/plans/2026-08-18-jarvis-voice-agent-plan.md`
