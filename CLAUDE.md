# CLAUDE.md

Jarvis: a Twilio phone + local wake-word voice agent, backed by the OpenAI
Realtime API and Claude Agent SDK subagents.

## Commands

- Run tests: `uv run pytest -q` (coverage: `uv run pytest -q --cov`, floor 94%)
- Lint: `uv run ruff check src tests`
- Run the CLI: `uv run jarvis --help`
- Check the machine's setup: `uv run jarvis doctor` (`--no-mic` where there is none)
- Run the agent: `uv run jarvis serve` (`--no-phone` / `--no-wakeword` /
  `--fake-agents` / `--host` / `--port`); `scripts/dev.sh` adds the Cloudflare tunnel
- Approval bridge: `uv run jarvis approvals` (`--limit N`, `--disable` / `--enable` for
  the kill switch); install the Claude hook with `scripts/install-claude-hook.sh`
- Inspect tasks: `uv run jarvis tasks list [--status …] [--limit N] [--internal]`,
  `uv run jarvis tasks show <id>` (the `TOLD` column is `NO` until Jarvis has said it)
- Read what Jarvis remembers between calls: `uv run jarvis memory` (`--path` for the file)
- Delete transcripts and finished task rows: `uv run jarvis forget [--older-than N]`
  (`--transcripts-only` / `--tasks-only` / `--yes`)
- Restart the service: `uv run jarvis restart [--reason …] [--force] [--no-callback]`
  (it phones back when it is up again, and texts if it never comes back);
  `uv run jarvis restart --status` for the last one, including what the logs said
- One-off setup: `uv run jarvis download-models`, `uv run jarvis setup-google`
- Background service: `scripts/install-systemd.sh [--uninstall]` on Linux,
  `scripts/install-launchd.sh [--uninstall]` on macOS

## Layout

Source lives under `src/jarvis/` (installable package, `src/` layout). Tests
live under `tests/`, mirroring the package structure. `cli.py` stays argument
parsing plus wiring: the `doctor` checks live in `jarvis/doctor.py` and the
Google OAuth bootstrap in `jarvis/google_setup.py`. Service templates are in
`ops/systemd/` (Linux) and `ops/launchd/` (macOS), rendered by the matching
`scripts/install-*.sh`; `scripts/lib.sh` holds the scaffolding those scripts share
(argument parsing, the env-file and PATH checks, `render`), so an installer is only
its platform-specific half.

Four groups are named here because the file you want is rarely the one whose name you
remember:

- **restart** — `restart/` is the whole subsystem: `coordinator`, `service`, `store`,
  `version`, `watchdog` and `logscan`. The directory listing is the index now.
- **tools** — `tools/builtin.py` is a composition root; the registrations are in
  `builtin_comms`, `builtin_billing`, `builtin_tasks`, `builtin_restart` and
  `builtin_session`, with the wording, the parsing and the two gates (`pin_gate`,
  `get_task`) in `builtin_common`. **The order `builtin.py` calls them in is the order
  the tools are offered to the model.** A new tool goes in a domain module and the README
  table, or `tests/test_docs_sync.py` fails.
- **notify** — `notify/deliver.py` holds `announce_to_live_sessions` and `safe_send_sms`.
  The `can_text` gate is asserted there and nowhere else.
- **integrations** — `integrations/` is one module per outside service (`billing`,
  `cluster`, `slack`, `web_search`), each behind exactly one voice tool. The tool's
  *registration* goes in `tools/builtin_<domain>.py`; its *client* goes here.

`logging_util.mask_number` is the only shape a phone number may take in a log line, and
`retention.py` is the transcript and task pruning (off by default).

## One task kind

There is one `TaskKind` (`agent`) and no per-kind tool restriction: every subagent gets
the full built-in tool set, the Google MCP server, the installed skills and subagents of
its own, and decides for itself what a request needs. The voice model's only routing
decision is answer-it-myself (small facts go through its `web_search` tool, backed by the
Responses API) versus dispatch. Do not reintroduce kinds to express "this one is
read-only" — the phone PIN gates every dispatch instead.

## Continuity is three pieces

A realtime session starts blank — the provider keeps nothing across sockets — so what
Jarvis knows at the top of a call is assembled every time by `jarvis/briefing.py`:

- **The digest.** `Task.reported_at` is the only record that Jarvis *told him*; `announced`
  and `sms_sent` only say a delivery was attempted, and neither survives a call he missed.
  Until `reported_at` is stamped, the task rides at the top of the next call. Exactly one
  thing stamps it: the voice model's `mark_reported` tool, after it has spoken the result.
  Do not stamp it from a delivery path — hearing something twice is recoverable, never
  hearing it is not.
- **The memory.** `jarvis/memory.py` subscribes to `SessionEnded` and dispatches a subagent
  (`prompts/memory_update.md`) that folds the call into `data_dir/memory.md`; the next call
  reads it back. Its headings are nested one level when embedded, so its sections cannot be
  mistaken for instructions.
- **`recall`.** `jarvis/recall.py` searches past transcripts and past task summaries on
  demand. Matching stays literal on purpose: the query is speech that transcription has
  already mangled once, and a fuzzy hit gets read out as if it were fact.

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
(`jarvis restart --status`).

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

Everything else in Jarvis carries a result *outwards* from work he asked for.
`jarvis/approvals/` is the opposite: a Claude Code session on his own screen has stopped
and asked *him* something, and he is not at the keyboard. A hook in `~/.claude/hooks/`
(canonical copy: `scripts/claude_hooks/jarvis_approval.py`, installed by
`scripts/install-claude-hook.sh`) hands the pending prompt to the broker over a Unix socket
and blocks; five minutes later, if he still has not answered, Jarvis rings him.

Four rulings hold it up, and none of them is a preference:

- **A Unix socket, never an HTTP route.** `cloudflared` puts the whole of port 8080 on the
  internet. `data_dir/approvals.sock` at 0600 is unreachable through it by construction.
- **`policy.py` is the *primary* control, not a second layer.** A `PermissionRequest` hook
  returning `allow` appears to skip the CLI's own `permissions.deny` re-check, so whatever
  `classify` calls eligible is what a keypad digit can run. It is an allowlist, it starts
  small, and the denylist wins over it. Do not widen it without saying why in the commit.
- **The keypad decides, never the transcription.** `answer_approval` cannot answer
  anything; the most it does is put a menu in the model's mouth. `ApprovalBroker.digit` is
  the only thing in Jarvis that can approve a tool call, it is reachable only after the PIN
  (`VoiceSession._on_dtmf` routes to it only once `authorized`), and an unrecognised key
  re-asks rather than agreeing.
- **Failure is always "do nothing".** Broker down, socket missing, Twilio broken, call
  unanswered, malformed reply, hook crash: all end with the hook printing nothing, which
  leaves the ordinary on-screen prompt exactly as it is. There is no path where an error
  approves something.

Pending is a fact to be re-checked, never assumed: the hook is *not* killed when he answers
at the keyboard, so `PostToolUse`/`PermissionDenied`/`Stop`/`SessionEnd` cancel the
escalation, and pending is re-read before dialling and again before any verdict is applied.
A prompt that arrives while he is already on the phone is announced into that call rather
than ringing him a second time. `uv run jarvis approvals` is the audit trail and
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

`jarvis/integrations/cluster.py` answers "what's free on alpha" and "am I still running on beta" from
Slurm, behind the voice model's `cluster_stats`. Three rulings, and the first is the one
with a scar behind it:

- **Never our own connection to the cluster.** Auth is Duo 2FA behind an ssh ControlMaster
  that lasts about twelve hours, and no non-interactive process can answer a Duo push: a
  direct attempt against a dead master *hangs*, and a storm of those retries is what got
  this machine's IP fail2ban-banned. Everything goes through the cluster-compute skill's
  guard (`CLUSTER_SSH_GUARD`, default `~/.claude/skills/…/cluster_ssh.sh`), which probes
  the local control socket — no network, no auth attempt — and exits `42`. That `42` is
  terminal: nothing retries it, and a missing guard is `not_configured`, never a fallback
  that dials out by itself. `CLUSTER_SSH_NO_NOTIFY=1` is set because the guard would
  otherwise Slack him unasked, and he is on the phone, which is where the sentence belongs.
- **Read-only by construction.** `build_script` assembles the remote command from module
  constants and refuses any command whose first word is not in `READ_ONLY` (`squeue`,
  `sinfo`); there is a test named after it. The only thing the model chooses is a cluster
  *name*, looked up in `CLUSTERS` and refused when it is not there — no string from the
  model reaches a shell. Un-PIN-gated for the same reason as `check_billing`, and the
  payload is counts plus his own job ids: no job name, no path, no other user.
- **Idle, planned and down are three numbers, not one.** `sinfo` without `-N` aggregates by
  state line and its totals are silently wrong; a `planned` node is backfill holding
  hardware for a queued job, not a free one; and most pending jobs are blocked on a
  dependency rather than competing for GPUs. The fixtures in `tests/test_cluster.py` are
  real cluster output, and the totals asserted on are what the skill's own `cluster_avail.py`
  reported for the same moment. Do not collapse them to be brief.

## Platforms

macOS runs both channels; Linux runs the phone channel only, because openwakeword
needs `tflite-runtime`, which has no cp312 wheel. `sounddevice`, `openwakeword` and
`onnxruntime` are therefore `sys_platform == 'darwin'` dependencies and a Linux host
serves with `--no-wakeword` — one more reason every import of them stays lazy.

## Testing rule

No network or hardware access in tests. OpenAI, Twilio, sounddevice,
openwakeword, and the Claude Agent SDK are always accessed through an
injectable interface (a `Protocol`) with a fake/test double used in tests —
never the real network or hardware. Heavy/hardware imports (`sounddevice`,
`openwakeword`) must be guarded inside functions, not imported at module
scope, so the test suite can run on a machine with no mic.

## Working agreements

- Only scripts read the env file; never print or paste its contents. `.env.example` is a
  different thing — tracked, secret-free, and the one place every setting is listed; keep
  it in step with `Settings` (`tests/test_docs_sync.py` is meant to enforce that, and the
  `.env.example` half of it is still missing — see issue #5).
- The database runs ahead of the code. `_migrate` upgrades `tasks.db` from whichever process
  opens it first, and `jarvis serve` holds the `Task` it imported at startup, so a new column
  reaches the file while the service is still a build behind. `Task.from_row` drops columns it
  has no field for; keep it that way, and keep writes naming their columns so the older build
  cannot blank the newer one's data.
- Jarvis does not text. `SMS_ENABLED` is false (the account has no SMS geo-permission for
  his region, and Slack is the written channel he actually asks for), so gate any send on
  `TwilioOut.can_text` and never on `configured` — outbound *calls* are unaffected, and the
  restart watchdog's `<Say>` alert is the last thing working when Jarvis is down.
- Spec §3.2 interface names and signatures stay stable (extra optional keyword
  arguments are fine). §3.3/§4 hold rulings: follow them, and amend the spec in a
  docs commit when one changes.
- Conventional commits (`feat:`/`fix:`/`chore:`/`docs:`) with the Co-Authored-By
  Claude trailer. A subagent Jarvis dispatched adds `Jarvis-Task: <id>` as well, so
  `git log --grep '^Jarvis-Task:'` is everything William asked for out loud rather than
  typed — the one thing `git log` cannot otherwise recover.
- Clean and minimal over clever; TDD, with `uv run pytest -q` and
  `uv run ruff check src tests` pristine before a commit. Coverage has a floor (94%) and it
  is a ratchet: raise it when the measured number moves up, never lower it to pass.
- `data_dir` is 0700 and the files under it 0600 (`config.secure_dir` / `secure_file`).
  Anything new that writes there goes through them.
- A configured `JARVIS_PIN` is 6-8 digits and `jarvis serve` refuses to start otherwise.

## Reference docs

- Design spec: `docs/superpowers/specs/2026-08-18-jarvis-voice-agent-design.md`
- Implementation plan: `docs/superpowers/plans/2026-08-18-jarvis-voice-agent-plan.md`
