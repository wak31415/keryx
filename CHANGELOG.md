# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [semantic versioning](https://semver.org/) over the CLI and `.env`
surface — a removed or renamed setting or command is a major bump.

## [Unreleased]

### Added

- **`jarvis init`** starts the memory before the first call: it asks what Jarvis should call
  you and a few things it should know, shows the `memory.md` it will write (owner-only), and
  reports what every call will carry to the realtime provider — memory, project briefs,
  skills. It never edits `.env`; it prints the `OWNER_NAME=` line to add. The README has a
  new "Teaching Jarvis about you" section.
- **`OWNER_NAME`**: whom Jarvis works for, in the voice prompt, the subagent prompt, the
  memory's title and the Slack tool. Blank means "the owner".
- **Wrong PINs count across calls.** `PIN_FAILURE_LIMIT` (10) inside
  `PIN_FAILURE_WINDOW_HOURS` (24) locks PIN entry on every call for `PIN_LOCKOUT_MINUTES`
  (60), survives a restart, and tells you once; `MAX_PHONE_SESSIONS` (2) caps phone calls
  open at once. SECURITY.md explains the price: a caller who can spoof your number can keep
  your PIN locked.
- `jarvis --version`, and five soft `jarvis doctor` checks: owner name, memory seeded,
  projects root, and whether `cluster_stats` and `send_to_slack` are offered and why not.
- A licence (Apache-2.0), CI on Linux and macOS, `SECURITY.md`, `CONTRIBUTING.md`, a code
  of conduct, issue and pull-request templates, and Dependabot.
- `jarvis doctor` reports whether `~/.jarvis` is readable by anyone else, which service
  manager supervises the process (and what is unavailable when nothing does), and what is
  wrong with a malformed `JARVIS_PIN`.
- The README documents the approval bridge, what is stored on disk and what is sent to
  which third party, and the single-owner assumptions the deployment rests on.
- Test coverage is measured (94%) and CI fails below that floor.
- Retention: `TRANSCRIPT_RETENTION_DAYS` and `TASK_RETENTION_DAYS` (both off by default)
  prune at the top of `jarvis serve`, and `jarvis forget` does it on demand. A finished
  task you have not been told about is never deleted.
- **A get-to-know-you first call.** With nothing in `memory.md` yet, the first authorized
  call opens as an introduction rather than an ordinary call: what Jarvis is, then what to
  call you, what you work on, which projects matter, how you like to be answered and what
  is worth ringing you about — a handful of questions, one a turn, and the shape of it said
  back once at the end. Work always comes first, "not now" ends it for the rest of the
  call, and it is never a condition of anything. The absence of the memory is the only
  marker, so the call after it is ordinary again. It lives in `prompts/first_call.md` and
  reloads without a restart.
- **`jarvis init --json`** prints the same report as one document, for the agent you told
  to set this up: the memory's size and the briefs' total against their caps, the projects
  and which of them wrote a brief, the skills, whether `OWNER_NAME` is set, and the `.env`
  line to add. It needs `--yes`, and the exit code is a contract — 0 written or nothing to
  write, 1 a memory was wanted and not written, 2 a wrong command line.
- **A `jarvis-onboard` Claude Code skill** (`skills/jarvis-onboard/`, copied or symlinked
  into `~/.claude/skills/`): run once at the keyboard, it interviews you, drafts a
  `.jarvis-brief.md` for the projects **you pick after seeing the list**, proposes additions
  to `~/.claude/CLAUDE.md`, and pipes the agreed facts into `jarvis init --from - --yes`.
  Nothing is scanned, written or sent without you seeing it first.

### Changed

- **Before the PIN, the phone gets nothing and keeps nothing.** Caller ID is spoofable, so
  an allowed number no longer earns anything private. On the phone, the memory, the unheard
  results, project names, past calls and pending approvals wait for the PIN; nothing is
  announced into a call that has not given it (and such a call never counts as having told
  you); every tool but `check_billing`, `cluster_stats`, `web_search`, `submit_pin` and
  `end_session` asks for the PIN first; no memory update runs after it; and `recall` never
  searches its transcript. Keying the PIN at the top of a call delivers the news straight
  away.
- **The approval bridge decides on exactly what will run.** The policy checks the raw
  command before normalising it, refuses anything the hook had to trim or cannot read back
  whole, matches `APPROVAL_BASH_ALLOW` word for word on the parsed argv (prefix entries
  such as `make` now match only exactly), allows only plain forms of `git push` and
  `git commit`, drops `pytest` from the defaults, and never lets a keypad approve a write
  into `.git`, `.claude` or `.mcp.json`. **Re-run `scripts/install-claude-hook.sh`**: an
  older installed hook makes every request ineligible.
- **`cluster_stats` is a worked example you configure** (`CLUSTERS`, `CLUSTER_SSH_GUARD`,
  both empty by default); the tool and its prompt paragraph appear only once both are set.
  **Breaking** for an install that relied on the built-in clusters.
- **The subagents' Slack MCP server is a setting** (`SLACK_MCP_SERVER`, empty by default).
  **Breaking** for an install that relied on the built-in server name.
- **`PROJECTS_ROOT` defaults to `~/projects`** and is never created; an unscoped task
  without one runs in `data_dir/workspace`. Set it if yours lives elsewhere.
- `SERVICE_MANAGER=auto` restarts through systemd or launchd only when this process runs
  under that unit, so a hand-started `jarvis serve` refuses instead of restarting the
  installed service. The installers render the installing shell's `PATH` and log under
  `DATA_DIR`: re-run them.
- The prompts, tool wording and docs say "the owner" and "they"; the prompt clock carries
  the time zone; with texting off, the voice model promises a call rather than a text;
  project briefs are capped at 6000 characters in total.
- The coverage floor is 95%.
- **Calls say a thing once.** Reviewing a week of transcripts turned up the same shape
  everywhere: "let me set that up for you" followed by "all set", four spoken turns for
  one PIN, a call-back that delivered its greeting twice. The voice prompt now forbids
  announcing an action and then confirming it, names the tools too fast to be worth
  announcing at all, and stops the model predicting the PIN or claiming to know what a
  running task will come back with. `mark_reported` and `end_session` are registered
  `silent=True`: their results no longer buy a spoken turn, which is what made a call-back
  repeat its own greeting.
- **The repository is now `jarvis-voice-agent`** (was `garmin-voice-agent`; GitHub
  redirects the old URL). The package, the CLI and `~/.jarvis` are unchanged.
- **`JARVIS_PIN` must be 6 to 8 digits.** `jarvis serve` refuses to start on anything else.
  An existing install with a shorter PIN must change it before deploying this.
- `~/.jarvis` and its subdirectories are created mode 0700, and transcripts, `tasks.db`,
  task logs and reports mode 0600. An existing tree is tightened in place on the next start.
- Every dependency now carries a version range instead of being unbounded.
- **A follow-up question is rare now, not forbidden.** The voice prompt used to ban one
  outright — "do not confirm first", "dispatch anyway", "the questions worth asking are the
  ones Claude works out". Dispatch-first is still the default, but the rule is a threshold:
  ask when the answer changes what actually happens and Claude could not work it out from
  the machine itself, roughly one dispatch in ten.

### Fixed

- `uv run jarvis serve` crashed on Linux unless given `--no-wakeword`; it now says the wake
  word needs macOS and serves the phone channel.
- A spoken PIN was written into call transcripts, where `recall` could read it back. It is
  now `[PIN]` as each line is written, and redacted again whenever an older transcript is
  read.
- The memory writer ran after every call, including one that never gave the PIN.
- Phone numbers reached the log in two more places (outbound call errors and the
  transcript header).
- `DEBUG_SKIP_TWILIO_VALIDATION` could run behind a public host; `serve` now refuses.
- A subagent that read a large image lost its turn to the Agent SDK's 1 MiB line limit.
- Read-only CLI commands printed the service's INFO log lines.
- The sdist shipped whatever happened to be in the checkout; it now ships an explicit list.
- Test fixtures and docs carried details of one person's install; they are synthetic now.
- Tests could read a developer's real `.env`, which once printed a live admin key into
  pytest output, and assumed the host they grew up on (systemd on `PATH`, no forced
  colour, a short temp directory). The suite now passes on Linux and macOS alike.
- `/openapi.json` was served through the public tunnel.
- Caller phone numbers were written to the log in full.
- The README listed 13 of the voice model's 19 tools, and the docs still described task
  kinds (`coding`, `cowork`) that were removed in August 2026.
