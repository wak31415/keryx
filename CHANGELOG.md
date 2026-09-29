# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [semantic versioning](https://semver.org/) over the CLI and `.env`
surface — a removed or renamed setting or command is a major bump.

## [Unreleased]

### Added

- **Your own voice tools.** A Python file in `~/.local/share/jarvis/tools/` defines a tool
  the voice model can call, with `@custom_tool` from `jarvis.tools.custom`; each call reads
  the directory afresh, so a new one needs no restart, and none of it lives in the
  repository. Ask for one out loud and the subagent writes it, following the new
  `skills/jarvis-custom-tools` skill. Each tool is behind the PIN unless it says `needs_pin=False`,
  cannot take a built-in's name, and is refused if anyone but you could write it.
  `jarvis tools` lists them and what the next call would refuse.
- **`jarvis migrate`** moves an install from `~/.jarvis`, and a `.env` or `.secrets/` in the
  checkout, to the XDG directories. `--dry-run` prints the plan, and a conflict stops it
  before anything is touched. It stops the service while it moves things, rewrites the
  paths `tasks.db` holds, imports the `.env`, re-renders the service and the approval hook,
  and starts the service again. Nothing is deleted: the old directory is renamed
  `~/.jarvis.migrated-<date>`. It can be run twice. Claude tasks that ran in the old
  workspace lose their session, and a follow-up starts afresh; the migration lists them.
- **`STATE_DIR`** (`~/.local/state/jarvis`): the logs, the restart record and stamps, and the
  approval bridge's socket and markers. **`CACHE_DIR`** (`~/.cache/jarvis`): the wake-word
  models, which `jarvis download-models` now fetches there instead of into the installed
  package. Neither may be changed by the running service.
- `jarvis config path` names the data, state and cache directories, and `--shell` prints
  them for a script to eval.
- **`jarvis setup`**, a wizard that asks only for what is still missing and saves as it
  goes: the voice key (checked with OpenAI), the coding agents and their sign-ins (never
  asked of an agent that can already run), your name, numbers and PIN, then — each optional —
  Twilio (numbers listed from your account; the webhooks set only after you say yes),
  Google, Slack, billing, a first memory, project summaries a coding agent drafts for you to
  accept, and the background service. `--all` reviews everything.
- **A configuration store.** Settings live in `~/.jarvis/config.toml` and every secret in a
  0600 `secrets.toml`; `jarvis config list|get|set|unset|path|import-env|lock|unlock` reads
  and changes them, and a secret is only ever taken from `--stdin` or `--from-env`.
  `docs/configuration.md` describes every setting, generated from the code.
- **`jarvis auth login claude|codex|gmail|google-workspace`** and `jarvis auth status`: every
  sign-in in one place. `--client-file` takes the Google client JSON the console downloads.
- **`jarvis setup --agent-instructions`**: how a coding agent sets Jarvis up from the command
  line; `skills/jarvis-setup` is the same as a Claude Code skill.
- **`set_config`**: ask Jarvis on a call to change its own voice, turn-taking or model. It
  may change only what the running service is allowed to; credentials, the PIN, who may call
  and every other line of defence are protected and cannot be unlocked.
- `jarvis doctor --json` and `--fix` (tightens loose secret files), and checks that secrets
  are private, outside git, not in `config.toml`, and that the Twilio webhook points here.
- `jarvis memory seed --file -` writes a first memory from standing facts.

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
- **A call Jarvis placed to your own number now counts for something.** Reaching
  `OWNER_NUMBER` means holding that phone, and the outbound call's media stream already
  carries a single-use token Jarvis minted, so such a call opens able to answer the
  question Claude came back with (`send_followup`), arrange a call back on that same
  number, mark a result as told, and answer a waiting approval on the keypad — without the
  PIN. Starting work, `recall`, restarting and the memory still need it. Nothing else
  confers this: not an allowed caller, not the `From` on an inbound call. Because voicemail
  can answer a call, acting on anything you *say* takes one keypress first. If an approval
  menu is open on such a call and you want the PIN, press `*` to give the keypad to the PIN
  (`*` again gives it back to the menu); saying the digits works at any time.
- **`BRIEFING_BEFORE_PIN`** (default `true`): an inbound call is handed its whole standing
  briefing before the PIN — the results you have not been told about, what Jarvis remembers
  about you, your project names, their briefs and your skills — and the four voice tools
  that read the same material back (`list_tasks`, `get_task_status`, `get_task_result`,
  `list_projects`) answer without it too. The trade-off is that a caller who spoofs one of
  your `ALLOWED_CALLERS` hears it; `false` restores the old silence and puts those four
  tools back behind the PIN with it.

- **The first call can set the PIN.** `JARVIS_PIN` is set at the keyboard, and until it is
  the phone is no use to you — the one setup step Jarvis cannot do for itself. So while
  there is no PIN at all, the first call may key one in: six to eight digits and hash,
  keyed a second time to confirm, written to `~/.jarvis/pin` (owner-only) and used from
  then on, briefing and all. It is a one-way door, held shut by the kernel: the file is
  created with `O_EXCL`, no tool or command anywhere can change an enrolled PIN, and only
  you can — in `.env`, which always wins, or by deleting the file. A spoken PIN cannot
  enrol one; the digits are keyed, checked twice and never reach the model, the transcript
  or a log line. The risk you accept is that whoever calls first sets it: the window is a
  few minutes long and closes on first use, and
  [SECURITY.md](SECURITY.md#setting-the-first-pin-on-the-first-call) has it in full,
  including what a subagent can still do to the file and what it cannot.
- **`jarvis init` suggests a PIN**: a `JARVIS_PIN=` line with six cryptographically random
  digits beside the `OWNER_NAME=` one, to paste or ignore — it still never edits `.env` and
  never sets a PIN itself. `--json` gains a `pin` block saying whether one is set, where it
  came from and where the file lives, and never the digits. `jarvis doctor` reports the same
  four states, and tells you to copy an enrolled PIN into `.env` to make it permanent.

### Changed

- **Storage follows XDG, on Linux and macOS alike.** The configuration, the PIN and the
  Google client file are in `~/.config/jarvis` (`JARVIS_HOME`), the data in
  `~/.local/share/jarvis` (`DATA_DIR`), the state in `~/.local/state/jarvis` and the cache in
  `~/.cache/jarvis`; each `XDG_*_HOME` is honoured. The PIN moved out of `DATA_DIR`, so
  that where it is no longer depends on a setting. A relative `DATA_DIR` is refused. The
  service units and the restart watchdog carry the resolved directories, the approval hook
  is always told where the socket is (`JARVIS_STATE_DIR`), and `scripts/dev.sh` logs the
  tunnel to `STATE_DIR/logs` rather than the checkout. `jarvis serve`, and every command
  that reads the data, refuses to start until `jarvis migrate` has run on an older install.
- **Before the PIN, the phone keeps nothing and changes nothing.** Caller ID is spoofable,
  so an allowed number no longer earns the right to *do* anything. On the phone, dispatch,
  `recall`, Slack, cancelling, restarting, arranging a call back and answering a pending
  approval all ask for the PIN first; no memory update runs after such a call, so a call
  that heard the memory read out still cannot rewrite it; and `recall` never searches its
  transcript. A call that has proved nothing still never counts as having told you.
- **The PIN is the line between reading and acting, not between private and not.** It
  defends against somebody spoofing one of your `ALLOWED_CALLERS`; it is not a defence
  against a compromised machine, which has `.env` and so has the PIN itself. Gating reads
  bought nothing against that attacker and charged a keypad entry to every ordinary call,
  so reads now happen before the PIN (`BRIEFING_BEFORE_PIN`, above). `recall` is the one
  read that stays behind it: the briefing is a bounded page you can read with
  `jarvis memory` and prune, where `recall` is an unbounded search of every call ever
  recorded, steered by whoever is on the line.
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
- **Trust on a call is three levels, not one bit.** `jarvis/trust.py` names them — nothing
  proved, a phone Jarvis dialled, and the PIN — and the voice prompt says which one this
  call is at and how to reach the next. Hearing a result announced mid-call no longer
  counts as having told *you* unless the call proved at least that much, so the text and
  the call-back still go out to a call that has not.

- **Before a PIN exists at all, nothing of yours is read out.** `BRIEFING_BEFORE_PIN`
  trades a read against a keypad entry, and that presumed there was an entry to make: on a
  machine that had never had a PIN there is no authentication on the phone, so an allowed
  caller heard the memory, the unheard results and your project names on every call, for
  ever, with no way to gate it. The briefing is withheld and the four read-only voice tools
  are refused until a PIN exists, whatever the setting says — which also makes `JARVIS_PIN`
  optional in `.env.example` rather than required.

### Removed

- **A `.env` in the working directory is no longer read**, nor
  `.secrets/client_secret.json`: a checkout is the one place a secret must never live.
  `jarvis migrate` moves both into the store.
- `jarvis init`, `setup-agent`, `setup-google` and `setup-gmail`: `jarvis setup`, `jarvis auth`
  and `jarvis memory seed` do what they did. `.env.example` is gone; an existing `.env` is
  still read below the store until `jarvis config import-env` moves it in.

### Fixed

- `jarvis doctor` died with a pydantic traceback when `JARVIS_PIN` was set to something
  that is not 6–8 digits — the one state it exists to explain, since `jarvis serve` will
  not load at all. The fallback it has for that matched the field name and never the
  `JARVIS_PIN` the error actually carries.
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
