# CLAUDE.md

Jarvis: a Twilio phone + local wake-word voice agent, backed by the OpenAI
Realtime API and Claude Agent SDK subagents.

## Commands

- Run tests: `uv run pytest -q`
- Lint: `uv run ruff check src tests`
- Run the CLI: `uv run jarvis --help`
- Check the machine's setup: `uv run jarvis doctor` (`--no-mic` where there is none)
- Run the agent: `uv run jarvis serve` (`--no-phone` / `--no-wakeword` /
  `--fake-agents` / `--host` / `--port`); `scripts/dev.sh` adds the Cloudflare tunnel
- Inspect tasks: `uv run jarvis tasks list [--status …] [--limit N] [--internal]`,
  `uv run jarvis tasks show <id>` (the `TOLD` column is `NO` until Jarvis has said it)
- Read what Jarvis remembers between calls: `uv run jarvis memory` (`--path` for the file)
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
`scripts/install-*.sh`; `scripts/lib.sh` holds what those scripts share.

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

The process that runs `systemctl restart` is the one that gets killed, so `jarvis/restart.py`
splits the flow across that death and joins it with `data_dir/restart.json`: `request()` writes
the record and hands over, `resume()` (one task per `jarvis serve`) finds it on the far side and
rings back with a status summary. Neither half may interrupt a call — a restart asked for during
one waits for the line to clear, and the confirmation is announced or texted rather than dialled
into a live session. Keep it that way, and keep every failure path landing somewhere a human can
find it (`jarvis restart --status`).

The third half is `jarvis/restart_watch.py`, and it exists because the first two both live
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
is killed handing over and returns `-15`. And "back up" is not "working" — `jarvis/logscan.py`
scopes the service's log files by byte offset (`marks()` before, `errors_since()` after) so the
confirmation can say what broke, and those errors are spoken *before* the housekeeping.

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

- Only scripts read the env file; never print or paste its contents.
- Spec §3.2 interface names and signatures stay stable (extra optional keyword
  arguments are fine). §3.3/§4 hold rulings: follow them, and amend the spec in a
  docs commit when one changes.
- Conventional commits (`feat:`/`fix:`/`chore:`/`docs:`) with the Co-Authored-By
  Claude trailer.
- Clean and minimal over clever; TDD, with `uv run pytest -q` and
  `uv run ruff check src tests` pristine before a commit.

## Reference docs

- Design spec: `docs/superpowers/specs/2026-08-18-jarvis-voice-agent-design.md`
- Implementation plan: `docs/superpowers/plans/2026-08-18-jarvis-voice-agent-plan.md`
