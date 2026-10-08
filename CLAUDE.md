# CLAUDE.md

Keryx: a Twilio phone voice-assistant service, backed by the OpenAI Realtime API and Claude
Agent SDK subagents. The assistant it puts on the line has a name of its own
(`ASSISTANT_NAME`, Lyra by default; see "Two names"). Until 2026-09-29 the service was called
Jarvis. The local wake-word channel is developed on `feat/local-wakeword` and is not part of
`main`.

## Commands

- Install: `uv sync` (every coding agent, via the default `agents` group); one agent only:
  `uv sync --no-group agents --extra codex` (or `claude`); with pip, `keryx[all|claude|codex]`
- Run tests: `uv run pytest -q` (coverage: `uv run pytest -q --cov`, floor 96%)
- Lint: `uv run ruff check src tests`
- Run the CLI: `uv run keryx --help`
- Set a machine up: `uv run keryx setup [--all]` (the wizard: walks only the sections still
  missing, saves as it goes; needs a terminal). A coding agent: `uv run keryx setup
  --agent-instructions`, which prints the command-line path below
- The steps to a first working call, as the Mac app's Setup Assistant shows them:
  `uv run keryx setup plan [--json]` (a report, so it exits 0; it asks what `doctor` asks, and
  a migration still to run is its first step), `uv run keryx setup mark STEP [--json]` (an
  optional step skipped, or `pin` left to the first call; an unknown step exits 2),
  `uv run keryx setup profile [--json]` (the wizard's "About you" questions as data, in its
  order), `setup profile --preview|--apply [--json]` (the answers, one JSON object on stdin
  — never a terminal — composed and written by the wizard's own code in `setup/profile.py`:
  `--apply` is the owner's alone, marks the section walked, and exits 1 with nothing written
  when an answer is not one, 2 when stdin is not one JSON object), and the project-context
  step in three calls (`setup/project_stream.py` over `setup/project_context.py`):
  `setup project-context --describe [--json]` (its words, question and choices),
  `setup project-context --explore|--folder PATH --json` (the wizard's one read-only task,
  run as the service, as JSON lines — `dispatch`, `working` with the agent's progress
  redacted in its `detail`, `reading` — and `done` carrying the draft; nothing is written,
  and Cancel closes the agent's session before `cancelled` is told), and `setup
  project-context --apply [--json]` (what the owner kept, one JSON object on stdin, written
  as the wizard writes it: briefs, a new project only inside the folders read, facts into
  the memory; the owner's alone, marks the section walked)
- The phone PIN: `uv run keryx pin [--json]` (set or not, where, the enrolment door and the
  wrong-PIN lockout; never the digits), `uv run keryx pin set --stdin [--replace] [--json]`
  (the owner's alone: the new PIN twice on stdin, one per line)
- Check the machine's setup: `uv run keryx doctor [--json] [--fix]` (`--fix`
  only tightens secret files to 0600/0700; `--json` carries each check's `section`,
  `state` — ok / missing / failed — `title`, a setting's own when a key names the check, and
  `unknown`, true for one that could not ask — Twilio not answering — which holds no section
  or setup step back, where Twilio refusing the credentials or a webhook elsewhere does; and
  `fix` and `hint`. The Mac app never shows a shell command, so a check's `detail` (and the
  plan's `why`) is written for a person — no command, no path, no traceback,
  `doctor.app_facing` — and what a terminal runs, or where a thing is on disk, is its
  `hint`, which `keryx doctor` prints after the detail. `fix` is the app's button for it: one
  id from the closed set in `keryx/fixes.py`, or null; the plan's steps and their checks
  carry it too, a step's being its first problem's)
- Whether it is running, and what it has to say: `uv run keryx status [--json]` (a report,
  so it always exits 0, a `keryx migrate` still to run included; `--json` is the document
  the Mac app's menu bar polls — `keryx/status.py`, schema 1, every part `ok`/`error` with
  its fields present either way)
- Settings: `uv run keryx config list [--json] [--group G]` (`--json` gives each row its
  `title` and `group_title`, the Mac app's `tab`, `tab_title` and `tab_order` — all three
  null for a row whose group an app screen owns, which names that `screen` — and whether
  it is `advanced`, its `type` — `keryx/config/types.py` — and a choice's
  `choices`, and `has_options`; the table keeps the keys, which are what is typed),
  `config options KEY [--json]` (the models or voices a setting can take, read live from
  OpenAI, Anthropic, Codex, a server of the owner's or the Twilio account when it can be,
  else Keryx's own list with `error` saying why — exit 0 either way;
  `keryx/config/options.py`), `config verify KEY [--stdin] [--pair] [--json]` (whether
  OPENAI_API_KEY, ANTHROPIC_API_KEY, CODEX_API_KEY or TWILIO_AUTH_TOKEN — with the saved SID,
  or with `--stdin --pair` the SID and token on two lines, neither saved, an SID not `AC` and
  32 hex exiting 2 before Twilio is asked — works, before it is saved: `--stdin` checks the
  typed value and saves nothing; a key that does not work is still exit 0, `problem` one
  sentence of cause and never the key; any other key exits 2;
  `keryx/config/verify.py`),
  `config get KEY… [--shell]` (never a secret), `config set KEY VALUE […]` (a secret only with
  `--stdin` or `--from-env VAR`), `config unset KEY…`, `config path [--shell]` (every
  directory), `config import-env [PATH]`, `config lock|unlock KEY` (what the running service
  may change)
- The phone without a terminal (the Mac app's phone steps; `keryx/phone.py`, which the
  wizard's phone section shares, its hostname rule and Twilio client included): `uv run
  keryx phone numbers [--json]` (the Twilio account's numbers, each with where its calls go
  and `points_here` — both webhooks here, as `phone webhooks` compares them, so the two
  screens agree; a report, so Twilio's refusal is its `error` and still exit 0 — `config
  options TWILIO_NUMBER` lists the same), `phone webhooks [--json]` (where `TWILIO_NUMBER`
  points against where it should, and `matches`), `phone webhooks --apply [--json]` (the
  owner's alone: points both webhooks at `PUBLIC_HOST` — the one write to Twilio, which the
  app confirms first, showing both addresses; exit 1 when Twilio refuses, 2 when something
  it needs is not saved or `PUBLIC_HOST` is not a bare hostname — `phone.hostname_problem`,
  the wizard's own rule, since the settings tab can save `https://…` and a webhook written
  from it would point somewhere else), `phone tunnel [--json]` (ngrok or cloudflared on
  PATH, as `doctor` finds them, its unit, and `reachable`: whether Keryx answers at
  `https://PUBLIC_HOST/health` within 3 s, null with no hostname, false with `error` for one
  that is not a hostname; `install_url`, cloudflared's download page while neither is
  installed, else null). The numbers are the owner's own, shown to the owner, and never
  logged but as `mask_number`
- Move a Jarvis install to Keryx — its XDG directories (`~/.config/jarvis`, …), or an older
  `~/.jarvis` and a checkout `.env`: `uv run keryx migrate [--dry-run] [--yes]` (stops the
  service, moves, retires `jarvis.service` for `keryx.service`, re-renders the unit and the
  approval hook, starts it again; `serve` refuses to start until it has run). For the Mac
  app: `migrate --json` (`--dry-run --json` is the same) is the plan as one document — the
  moves, the units stopped, retired and installed, the live calls, and each conflict as a
  sentence for a person with the terminal's detail beside it; a read, so exit 0 with a
  conflict in it — and `migrate --yes --json` the run as JSON lines (`setup/migrate_stream.py`,
  a `step` per phase of `config.migrate.run`, conflicts an error exit 2 before anything is
  touched). Cancel counts only before the first step: from the service's stop on, the run
  holds SIGTERM off and finishes, saying so, because a migration left half way leaves the
  service stopped; the app does not offer Cancel once a step has come
- Sign-ins: `uv run keryx auth login claude|codex|gmail|google-workspace [--headless]
  [--client-file PATH] [--callback-url URL]` (gmail is two steps: a link, then the address
  the browser landed on), `uv run keryx auth status [--json] [--smoke]`, `uv run keryx auth
  logout gmail|google-workspace [--json]` (the owner's alone: removes the tokens that one
  sign-in stored — never the OAuth client file, never the other sign-in)
- The same sign-ins from the Mac app: `uv run keryx auth login NAME [--client-file PATH]
  --json` (`keryx/setup/auth_stream.py`), one JSON-lines stream each: `open` (`url`, and
  Codex's `code`), Claude's `code` (the one line it then reads on stdin, as gmail reads the
  address), then one ending. Claude's drives `claude setup-token` behind a pty
  (`setup/terminal.py`) and stores the token it prints as `CLAUDE_CODE_OAUTH_TOKEN`; that
  screen carries the token, so it is never echoed, logged or quoted in an error — only fixed
  sentences, and a screen that no longer parses says "use an API key instead". A link is
  opened only when it is https on exactly the vendor's host (no user info, no port, nothing
  a browser reads differently), and every login child runs with no secret of Keryx's in its
  environment, whatever the case of its name (`keryx/child_env.py`, the workspace-mcp
  server's too); so does every operational child — speech-to-speech's download, the package
  installers, the checkout's unit installer — which keeps only the Hugging Face tokens
- The accounts Keryx connects to, as the Mac app's Integrations section shows them: `uv run
  keryx integrations [--json]` (google, gmail, slack, github, openai-admin, anthropic-admin:
  connected or not, `why` in the app's words, the secrets — set or not — and settings each
  card edits, its sign-in, and the plugins that use it; only reads, and asks `gh` only while
  issue reports are on; `keryx/integrations/accounts.py`)
- Run the agent: `uv run keryx serve` (`--demo` / `--host` / `--port`; a hidden
  `--no-wakeword` is accepted and ignored, for units installed before it left `main`); `scripts/dev.sh` adds the Cloudflare tunnel
- Approval bridge: `uv run keryx approvals` (`--limit N`, `--disable` / `--enable` for
  the kill switch, `--json` for the shape the Mac app reads — refused together with either
  switch); install the Claude hook with `scripts/install-claude-hook.sh`
- Inspect tasks: `uv run keryx tasks list [--status …] [--limit N] [--internal] [--json]`,
  `uv run keryx tasks show <id> [--json]` (the `TOLD` column is `NO` until the assistant has
  said it; `--json` on either is the shape the Mac app reads — `keryx/tasks/export.py` —
  and on `show` it also carries the task's agent session: its progress log, follow-ups and
  whether it is resumable)
- What the tasks spent, per project: `uv run keryx tasks usage [--days N] [--json]` (by the
  project each was dispatched with; the dollars are the agents' own estimates, and the
  voice calls are not tasks)
- Read what the assistant remembers between calls: `uv run keryx memory` (`--path` for the file;
  `--json` for `path`, `exists` and `text`, which is null when there is none or it cannot
  be read)
- The plugins (Slack, email, billing, cluster stats): `uv run keryx plugins [list] [--json]`,
  `plugins hosts [--json]` (the ssh hosts `cluster_stats` could ask), `plugins install NAME
  [--set KEY=VALUE …] [--cluster ALIAS=PARTITION …] [--guard PATH] [--template]`, `plugins
  install [NAME] --from-settings` (moves the settings they replaced), `plugins remove NAME
  [--yes]`, `plugins set NAME KEY=VALUE … [--cluster ALIAS=PARTITION …]` (an installed
  plugin's file settings, with install's validation, nothing reinstalled; not installed
  exits 1); install, remove and set refuse the running service, and each takes `--json`
  (`{name, on, message}`; remove then asks nothing). `plugins list --json` also describes
  each for the Mac app: `title`, `description`, typed `fields`, the integrations it
  `needs` (any one will do), and `ready` — would it work on a call once turned on, read
  beside `on` — with `why` in the app's words. `set` can repair a file that does not
  validate: the change goes over the raw file, and only a result that validates is written
  (one that does not parse is left for the owner, as the wizard leaves it)
- The owner's own voice tools: `uv run keryx tools [--json]` (what `DATA_DIR/tools` holds
  and what the next call would refuse; exits 1 while anything is)
- Start that memory before any call has: `uv run keryx memory seed --file FILE|- [--force]
  [--json]` (one fact per line; exits 1 only when a memory was wanted and not written, 2 on
  a wrong command line); `skills/keryx-setup` is the agent session that drives the whole
  command-line path
- Delete transcripts and finished task rows: `uv run keryx forget [--older-than N]`
  (`--transcripts-only` / `--tasks-only` / `--yes`)
- Restart the service: `uv run keryx restart [--reason …] [--force] [--no-callback]`
  (it phones back when it is up again, and rings with a plain spoken alert — texting too,
  when `SMS_ENABLED` is on — if it never comes back);
  `uv run keryx restart --status` for the last one, including what the logs said
- Regenerate `docs/configuration.md` with
  `uv run python -m keryx.config.reference > docs/configuration.md`
- Background service: `scripts/install-systemd.sh [--uninstall]` on Linux,
  `scripts/install-launchd.sh [--uninstall]` on macOS; `--llm` / `--voice` install the local
  model servers' units instead (`keryx models serve llm|voice`)
- The same without a terminal (the Mac app): `uv run keryx service [--json]` (`manager`, and
  each of `agent`, `tunnel`, `llm`, `voice` installed and active — asked, never changed;
  `keryx/units.py`), `service install [--json]` and `service uninstall [--llm] [--voice]
  [--json]` (runs the checkout's installer with those flags: `{"ok", "message"}`; exit 1 with
  the script's last error line, 2 with no checkout or service manager). `service install
  --llm|--voice` is refused, exit 2 (`{"ok": false, "message"}` with `--json`): `models
  start` and `voice use here` own those units — install, start, check, and the unit removed
  again when the server does not come up — so none is installed bare to restart for ever
- Models on this machine: `uv run keryx models list [--json]` (the catalog against this
  machine's GPU and memory, the recommended one marked, and the local voices; `--json` adds
  `servers`, the model and voice servers' units and whether they answer, asked on loopback
  only, and `served_model`, the model the running server names — `model` is the setting),
  `models pull NAME [--json]` (resumable, checksummed, into `CACHE_DIR/models`; a copy in
  the Hugging Face cache is linked instead; `--json` is JSON lines — `progress`, then
  exactly one of `done`, `adopted`, `error`, `cancelled` on every path, a refusal included —
  on this stream and every other JSON-lines one, `error` is `message`, one sentence for a
  person and never a tool's output, and `detail`, always there: null, or what the tool said,
  a path, the log (`EventLines.error`); a `step` event (a warning is one) is the same, a
  sentence and its `detail` (`voice.JsonSteps`);
  a SIGTERM cancels, exit 130, and keeps the `.part`), `models use NAME [--default-agent]
  [--json]` (a downloaded model as the local agent's: saves what the wizard would, through
  the same `localmodels/use.py`; a restart picks it up), `models start [--runtime
  llama|ollama] [--json]` (the chosen, downloaded model served here: llama.cpp installed
  when it is missing, the `--llm` unit, the wait for it, and a check that the server names
  that model — through the wizard's own `localmodels/server.py`; a server that does not come
  up, serves another model, or is cancelled while it loads has its unit removed first, and
  `--runtime ollama` removes an installed llama.cpp unit once Ollama serves the model;
  `--json` is JSON lines — `step` (`runtime`, `unit`, `start`, `check`), `progress` while
  llama.cpp downloads, then exactly one of `done` (`url`, `served_model`), `error`,
  `cancelled`; exit 2 until a model is chosen and downloaded), `models stop [--json]`
  (removes the model server's unit), `models serve llm|voice` (what the units run: it reads
  the settings and becomes the server; the voice server first waits, up to `LLM_START_S`,
  for the model server its words come from, so the two loading together at login is no crash
  loop)
- The voice on a call: `uv run keryx voice [--json]` (where it is — `mode` openai, here or
  server — and the voice server on this machine: installed, its unit, whether it answers on
  loopback, the local model its words come from, its `stt` and `tts` and whether their
  models are downloaded, `models_ready`; `needs` says `local_agent_model` while there is
  none; a server elsewhere is asked only while it is the voice, never with its key in the
  output), `voice download [--json]` (the speech models `VOICE_SERVER_STT`, `_TTS` and
  `_VOICE` need, fetched into the Hugging Face cache by speech-to-speech's own Python, which
  is installed first when missing — `localmodels/speech.py`; `--json` is JSON lines — a
  `step` (`install`, `download`), `progress` in bytes, then exactly one of `done` (`stt`,
  `tts`), `error`, `cancelled`; exit 2 for a speech model Keryx cannot name; `config options
  VOICE_SERVER_STT|TTS` offers only the ones it can name — never whisper-mlx, though one set
  before is kept — and marks the downloaded ones), `voice use openai [--json]`, `voice use
  server URL [--key-stdin | --clear-key] [--keep-anyway] [--json]` (saved only once it opens
  a Realtime session, unless kept anyway; exit 1 with the `problem` and nothing saved —
  `--json`'s `problem` a sentence for a person, what the server said its `detail`; the
  stored key is kept and asked with unless `--key-stdin` replaces it — shown as `(set)` — or
  `--clear-key` forgets it), `voice use here [--voice NAME] [--json]` (what the wizard does,
  through the same `localmodels/voice.py`: install speech-to-speech, download its speech
  models when they are not in the cache — `voice download`'s own step, `progress` and all,
  and a download that stops installs no unit — then its unit, wait for it, then save it;
  refused while the local model server its words come from does not answer, and a server
  that does not come up, or whose start is cancelled, has its unit removed first — as does a
  unit install that fails, times out or is cancelled — so no unit is left crash-looping;
  `--json` is JSON lines — a `step` before each slow one (`install`, `download`,
  `configure`, `unit`, `start`), `progress` while it downloads, then exactly one of `done`,
  `error`, `cancelled`; exit 2 for a refusal before anything is tried, 1 for what stopped
  it, 130 for a SIGTERM)
- The Mac app (`macos/`): `macos/scripts/test.sh` (`test-scripts.sh`, the scripts' tests in a
  temporary folder, then the Swift tests; it adds the Command Line Tools' Testing framework
  path only when they are the developer directory), `macos/scripts/build-app.sh
  [--no-install]` (builds `macos/build/Keryx.app` and replaces `/Applications/Keryx.app`, and
  nothing else there, by a copy beside it and two renames; signed with "Keryx Local Signing"
  when it is trusted in the login keychain, else ad hoc with a warning; never run without
  `--no-install` from a test or a subagent), `macos/scripts/make-signing-identity.sh
  [--yes] [--remove]` (the owner's, once: that identity, made and trusted for code signing in
  the user trust domain — codesign refuses it untrusted; `--remove` undoes both; `KEYCHAIN`
  and `KERYX_SIGNING_TRUST=0` are for tests), `macos/scripts/snapshots.sh [--offscreen]`
  (every screen, light and dark, into `macos/build/snapshots/`), and
  `swift macos/scripts/make-icon.swift` from the repository root (the two `.icns` in
  `macos/Resources`)

## Layout

Source lives under `src/keryx/` (installable package, `src/` layout). Tests
live under `tests/`, mirroring the package structure. `cli.py` stays argument
parsing plus wiring: the `doctor` checks live in `keryx/doctor.py`, everything behind
`keryx setup` and `keryx auth` in `keryx/setup/`, and the settings in `keryx/config/`. Service
templates are in `ops/systemd/` (Linux) and `ops/launchd/` (macOS), rendered by the matching
`scripts/install-*.sh`; `scripts/lib.sh` holds the scaffolding those scripts share
(argument parsing, `config_value` — every setting read through `keryx config get`, never a
grep — the PATH checks, `render`), so an installer is only its platform-specific half.

Eleven groups are named here because the file you want is rarely the one whose name you
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
  `tasks/agent_runner.py` only re-exports the original runner names. `docs/agents.md` is the parity
  matrix, and `tests/test_docs_sync.py` wants a column in it for every backend.

- **restart** — `restart/` is the whole subsystem: `coordinator`, `service`, `store`,
  `version`, `watchdog` and `logscan`. The directory listing is the index now.
- **tools** — `tools/builtin.py` is a composition root; the registrations are in
  `builtin_comms`, `builtin_tasks`, `builtin_restart` and `builtin_session`, with the wording, the parsing and the gates (`pin_gate`, `read_gate`,
  `possession_gate`, `get_task`) in `builtin_common`. **The order `builtin.py` calls them in is the order
  the tools are offered to the model.** A new tool goes in a domain module and the
  `docs/tools.md` table, or `tests/test_docs_sync.py` fails. A tool may also be registered `silent=True`
  (`end_session`): its result is submitted without asking for a response, because it is
  called *after* the thing worth saying has been said and the turn would only be spent
  saying it again. `mark_reported` is silent *per call*: the model says in `still_to_say`
  whether it has finished, because it calls it before the result as often as after — on
  almost every call-back of 2026-09-29, a line of greeting, the stamp, and silence until the
  owner asked what the update was. Only for those; anything the owner is waiting to hear
  keeps its turn.
  The owner's *own* tools are not here: `tools/custom.py` loads `DATA_DIR/tools/*.py` into
  each call's copy of the registry (`ToolRegistry.for_call`), so they need no restart and
  never enter the repository. Each declares its gate (`needs_pin`, True by default), cannot
  take a built-in's name, and is refused if anyone else could write it;
  `skills/keryx-custom-tools` is how a subagent writes one, and every subagent's prompt
  points at it. A file may raise `ToolUnavailable` while it loads to be refused with a
  reason, and reads its loader's settings from `LOADING_SETTINGS`.
- **plugins** — `plugins/` is the optional voice tools (`send_to_slack`, `check_email`,
  `check_billing`, `cluster_stats`), none registered from the source tree. `PLUGINS` in
  `plugins/__init__.py` is the table everything reads (its `important` keys are what the
  wizard asks; `retired` the settings each replaced; `needs` the integrations it reads), with
  the file helpers (`install`, `remove`, `write_config`, `status`, `move_from_settings`) and
  `describe`, the Mac app's view of one (its fields typed by `FIELD_TYPES`, and whether it
  is ready, with `app_sentence` keeping commands out of `why`). One module per plugin builds its
  tool (`<name>_tool(config_path, fake=…)`), `ssh_hosts` reads `~/.ssh/config`, and
  `templates/` is data — a one-line `.py` and a commented `.toml` per plugin, copied into
  `DATA_DIR/tools`, never imported from there. So a plugin *is* a custom tool: loaded per
  call, gated by `needs_pin`, no restart. The logic stays here with its tests so a fix
  reaches installed copies. The wizard's section is `setup/plugins.py`; `keryx plugins`
  is the command line.
- **notify** — `notify/deliver.py` holds `announce_to_live_sessions` and `safe_send_sms`.
  The `can_text` gate is asserted there and nowhere else. Slack reaches the notifier (the
  PIN-lockout alert) only through `plugins.slack.slack_route`, asked at alert time.
- **integrations** — `integrations/` is one module per outside service (`billing`,
  `cluster`, `gmail`, `slack`, `web_search`): the *clients*, each behind one voice tool —
  `web_search` behind a built-in, the rest behind a plugin, which is where the tool is
  built. Their builders take explicit values, never a plugin's file. `accounts` is the one
  that is not a client: the accounts `keryx integrations` lists, composed from `auth`'s
  Google entries, the settings and `issues.gh_status`, with `used_by` read from each
  plugin's `needs`.
- **config** — `config/` is the settings and where they live: `settings` (every field with
  a `description`, a `title`, a `group`, `advanced` and a default `service_writable`,
  declared with `setting(...)`; the title is the sentence-case name the Mac app shows in
  place of the key, `TABS` is the one table of the app's tabs and the groups each holds,
  `SCREEN_OF_GROUP` the groups an app screen owns instead (the Voice screen's
  `voice_model`, the Local Models screen's `model_servers`: each setting has exactly one
  editor), and `advanced` puts a field under its tab's or screen's "Advanced" disclosure),
  `store` (`KERYX_HOME/config.toml` and `secrets.toml`, the only writer of either),
  `permissions` (what the running service may change; `PROTECTED_KEYS`), `pin`
  (`KERYX_HOME/pin`), `files` (the XDG directories, the modes and atomic writes), `migrate`
  (`keryx migrate`) and `reference` (generates `docs/configuration.md`). The package
  re-exports the old `keryx.config` names.
- **setup** — `setup/` is `keryx setup` and `keryx auth`: `wizard` (section order, what is
  left, the closing summary), one module per large section (`agents`, `phone`, `google`,
  `issues`, `plugins`, `profile`, `project_context`) and `sections` for the small ones,
  `auth_stream` (`auth login --json`), `terminal` (the pty it drives Claude's sign-in
  behind), `child_env` (a re-export: `keryx/child_env.py` is every login and operational
  child's environment, scrubbed of Keryx's secrets), `context` (the `SetupContext` every section gets, and `Probes` — everything that reaches
  the network, a login or a subagent, replaced wholesale in tests), `ui` (the `Prompter` protocol and the
  rich/questionary one, one question to a screen), `rewind` (Esc goes back a question and
  Tab skips to the first unanswered one, both by running the section again from a record
  of its answers and probe results, so no probe runs twice), `auth`, `plan` (`keryx setup
  plan`/`mark`: the wizard's `section_statuses` cut into the app's steps), and `guides/*.md`,
  which the wizard renders and the wiki links, so each set of instructions is written once.
- **localmodels** — `localmodels/` is the model on this machine: `hardware` (the GPU, the
  memory, the disk), `catalog` (the models offered, with verified files and checksums, what
  fits, the recommendation, the Kokoro voices), `download`, `runtimes` (finding or installing
  llama.cpp and speech-to-speech, both pinned), `servers` (each server's command line from
  the settings, `free_port`, `wait_for`), `pull` (`keryx models pull`'s steps, told as a
  progress bar or as JSON lines; its `EventLines` is any stream that ends exactly once), `use`
  (the settings that make a model the local agent's — `llama_values`, `enable_local`, which
  the wizard and `keryx models use` both save — and the servers' status), `server` (the
  model server's steps — llama.cpp installed when missing, its `--llm` unit, the wait for
  it, or Ollama's pull — which the wizard and `keryx models start` both run) and `voice` (the
  voice's three places, its status, and the steps that set each up, which the wizard and
  `keryx voice use` both run: the wizard only asks and tells; and `keryx voice download`'s),
  `speech` (the Hugging Face repositories speech-to-speech fetches for each `--stt` and
  `--tts`, read from its own source and never guessed — one it cannot name is said, not
  fetched — whether they are in the cache, and the fetch) and `speech_fetch` (the script
  that fetch runs with speech-to-speech's own Python; Keryx never imports it to run it).
  `setup/local_models.py` is the wizard's section and `keryx models` and `keryx voice` the
  command line; every install, download and network read in the wizard goes through
  `Probes`, and in `keryx voice use` and `voice download` through `voice.machine()`.
- **continuity** — `continuity/` is what survives the end of a call: `briefing`, `memory`
  and `recall` (the three pieces below), plus `transcripts`, the call log they read, and
  `retention`, which prunes exactly those artefacts.
- **mac app** — `macos/` is Keryx for Mac, a SwiftPM package outside `src/`: `KeryxKit` (the
  runner, the locator, the client over the CLI, the decoded models and `AppModel`),
  `KeryxUI` (the screens and the menu), `KeryxApp` (the shell: the window, the menu bar item,
  the app's own Settings) and `KeryxSnapshots` (the harness that photographs every screen).
  It runs the `keryx` CLI and nothing else (but `scripts/install-launchd.sh --llm`, to start
  the model server), so every rule above binds it without a copy: a command it needs gets a
  `--json`, never a route or a socket, and it writes none of Keryx's files itself. While it
  is open Keryx answers calls (`AppModel.keepsAnswering`, the owner's ruling): once a launch
  it runs `keryx service install --json` when `setup plan`'s service step is reachable and
  Keryx is not running (never before `migrate`, never beside a hand-started `serve`), and
  Quit runs `keryx service uninstall --json` — the CLI's only stop until a `service stop`
  exists (`AppModel.stopUnits` is the seam), so it never calls `launchctl`; a logout lets the
  units go untouched — while closing a window leaves both running: it is a regular Dock app, no
  `LSUIElement`. The service is never shown as one ("Answering calls", "Start Keryx"). A
  `KERYX_APP_ENVIRONMENT=inherit` run neither starts nor stops it, and the locator reads its
  remembered copy of `dev.keryx.agent.plist` while quitting has removed the real one. It strips
  `KERYX_ACTOR` and `JARVIS_ACTOR` (`Sanitise.environment`), because it is the owner at a
  terminal and never the service. It finds Keryx the way the service runs — the folder
  chosen in its Settings, then `~/Library/LaunchAgents/dev.keryx.agent.plist`'s program,
  working directory and environment, then a login shell — with a checkout optional, and
  runs each command in the checkout, else in `/` as launchd does (never `HOME`, where an
  unrelated `.env` would make the CLI refuse). `KERYX_APP_ENVIRONMENT=inherit` in the app's
  own environment, for sandbox and development runs only, replaces the login shell with that
  environment and skips the launchd plist. It sets the PIN only through `keryx pin set
  --stdin [--replace]`, with the CLI's own safeguards: the owner's alone, digits typed twice
  and compared, never shown or logged, a TTY refused, replacing only when asked; it cannot
  approve anything (it can pause the approval calls). A secret goes on stdin and nowhere
  else (`config set|verify KEY --stdin`, `pin set --stdin`, a sign-in's code). Its command log is an allowlist (`CommandLog`): the subcommand, its verb and its
  flags from fixed lists, every other word `…`, then the exit code and the duration;
  stdin, the environment and the output never. The snapshots photograph only the app's own
  windows, by window id. `tests/test_macos_contract.py` holds the JSON contract — the keys
  the Swift decoders require from each command, with their JSON types, and the events,
  ending and exit codes of its streams (`models pull`, `voice use here`, `voice download`,
  `models start` and the four `auth login NAME`, each `--json`) — so
  renaming a field fails pytest, not the app at runtime.

`logging_util.mask_number` is the only shape a phone number may take in a log line.

## One task kind

There is one `TaskKind` (`agent`) and no per-kind tool restriction: every subagent gets
the full built-in tool set, the Google MCP server, the installed skills and subagents of
its own, and decides for itself what a request needs. The voice model's only routing
decision is answer-it-myself (small facts go through its `web_search` tool, backed by the
Responses API) versus dispatch. Do not reintroduce kinds to express "this one is
read-only" — the phone PIN gates every dispatch instead.

## Two harnesses, three agent names, one of them per task

`AGENT_BACKEND` (`claude`, `codex` or `local`) runs what nobody named an agent for;
`AGENTS_ENABLED` is what the voice may name. `local` is a model on a server of the owner's
own, run *inside* one of the two harnesses — Claude Code for a server that speaks
Anthropic's Messages API, Codex for one that speaks the Responses API (`LOCAL_AGENT_API`) —
by pointing it at `LOCAL_AGENT_BASE_URL` (`ClaudeAgentRunner`/`CodexAgentRunner` with an
`endpoint`). Its `BackendSpec.harness` says which, and `registry.installed`, `cli_path`,
`install_command` and `instructions_file` ask the harness. It has no sign-in: `AuthMode.ENDPOINT`
is ready once it has an address. Six rulings:

- **A task keeps its agent for life.** `Task.agent` is fixed at dispatch, and every resume
  opens on it, never on today's default: a session id belongs to the agent that issued it.
  The column `claude_session_id` holds whichever agent's id, and keeps its name for the
  build running behind the database.
- **A credential goes in the child's environment and nowhere else** — not argv, not a log,
  not a spoken error (`agents/auth.redact`, and `AdapterSession` redacts every error it
  returns or logs of the credential *and* of every MCP secret the agent was handed). What a
  tool, a server or an exception said reaches a `detail`, a `hint` or a problem only through
  `child_env.redact_known`, which takes out every secret setting's value, every credential
  variable in the environment, the Hugging Face tokens, and any key in hand. One
  exception, forced by Codex's app-server ignoring `CODEX_API_KEY`: that key is logged in
  once, on stdin, into `data_dir/codex`, never the owner's `~/.codex`. `OPENAI_API_KEY` is
  never lent to Codex: that would move a ChatGPT-plan user onto per-token billing unasked,
  and because the SDK copies Keryx's environment into the app-server, every credential
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
- **A local server is never handed the owner's credentials.** Claude Code gets
  `ANTHROPIC_BASE_URL` and the local key (or `PLACEHOLDER_BEARER`) as `ANTHROPIC_AUTH_TOKEN`,
  with `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` blanked and every model family name
  set to the local model (`claude.local_env`; a test is named after it). Codex gets the server
  as the thread's `keryx_local` model provider, the key only as `KERYX_LOCAL_API_KEY` in the
  child's environment, and a `CODEX_HOME` of its own (`data_dir/codex-local`). The dollar cap
  and the cost are dropped for it, since the SDK would price local tokens at Anthropic's
  rates. Never Codex's `--oss`, which ignores a remote base URL.
- **Prompts say "Claude", and code names the agent.** Templates are live under whatever build
  is running, which blanks a placeholder it does not know, so the voice prompt's own wording
  is rewritten to the default agent's name in `prompts._name_the_agent` rather than templated.
  The one new placeholder, `{agents}`, is a paragraph of its own that blanks cleanly.
  `{assistant}` is the exception that proves it: it arrived with the rename, when the
  templates moved to `src/keryx`, where no older build reads them at all.

## A model is an address

Every model Keryx talks to is a `keryx/endpoints.py::Endpoint` — the `…/v1` root of a server,
an optional Bearer key, the server's own name for the model — which is how every
OpenAI-compatible client describes one. Five rulings:

- **No provider library in Keryx.** The harnesses carry theirs; Keryx only says where.
  `endpoints.py` is also the one place the localhost-versus-server rule lives: a private
  host (loopback, RFC 1918, a tailnet, `.local`, a bare name) may run without a key and is
  sent `PLACEHOLDER_BEARER` where an SDK refuses none; a public one is *warned* about in
  `doctor` (`endpoints.warnings`), never refused.
- **One voice client.** `OpenAIRealtimeClient` speaks to `Settings.voice_endpoint`, which
  `Settings.voice_mode` chooses: `VOICE_MODE`, else worked out from `VOICE_BASE_URL` exactly
  as before it was a setting (blank is `openai`, the loopback address of `VOICE_SERVER_PORT`
  is `here`, anything else `server`). `here` is Hugging Face's speech-to-speech, which
  `keryx setup` installs, at an address derived from its port; `server` is `VOICE_BASE_URL`,
  and None without one, which `voice_refusal` refuses. `realtime.make_provider` is the only
  place one is built, for the phone and `keryx loopback` alike. Two differences are made for
  a server that is not OpenAI's, because they were measured to matter (2026-10-04): a call is
  transcoded µ-law 8 kHz ⇄ PCM16 24 kHz (`audio/codec.py`), and a note goes in as a `user`
  item marked as not the caller's — that server takes a `system` item as a *new system
  prompt*, which would wipe the voice prompt. Do not add a third without measuring it.
  Truncate is accepted and ignored there, and that is accepted, not worked around. Only
  OpenAI's own host is sent the persona's voice and `OPENAI_TRANSCRIPTION_MODEL`
  (`Settings.voice`, `voice_transcription_model`), since a server that does not know `marin`
  speaks nothing. The pipeline here is sent no voice — it speaks `VOICE_SERVER_VOICE` from
  its command line, and a per-session voice would override that — and no `?model=`. A
  server elsewhere is still sent `OPENAI_REALTIME_MODEL`, which it may choose its model by,
  and `VOICE_SERVER_VOICE` when that was set, else `OPENAI_VOICE` for one release.
- **Every `*_BASE_URL` is protected**, and `VOICE_SERVER_ARGS`, `VOICE_MODE` and
  `VOICE_LLM` with them. A voice address pointed elsewhere ships every call's audio, the
  spoken PIN included, to whoever is there, and a mode or a voice-LLM source can send the
  same audio or the same words elsewhere without an address changing; a flag could bind the
  unauthenticated voice server beyond 127.0.0.1.
- **The servers on this machine are the settings' to describe.** The `keryx-llm`,
  `keryx-voice` and `keryx-voice-llm` units run `keryx models serve llm|voice|voice-llm`,
  which builds the command line from `AGENT_SERVER_*` / `VOICE_SERVER_*` / `VOICE_LLM_*` and
  `exec`s it, so no unit holds a model, a port or a voice. All bind 127.0.0.1; the two
  llama.cpp ones are one `llm_argv` with a model and a port passed in. The voice server's
  words come from the voice LLM (`Settings.voice_llm_endpoint`, by `VOICE_LLM`: the local
  agent's while `shared`, which is byte-identical to before it was a setting, its own unit's
  with `here`, an address with `remote`), and it waits for that server, and `voice use here`
  refuses while it does not answer. Its key goes in the child's `OPENAI_API_KEY` in place of
  the owner's — never on argv. Setup saves `VOICE_BASE_URL` only after the server has
  answered a Realtime handshake. `units.KINDS` (with `localmodels.use.UNITS`) is the one list
  of units; `service install` refuses each model server's, which the command that starts and
  checks it installs. The three ports are kept apart, since a free port may be one a server
  not yet running is set to: each search steps past the other two
  (`servers.free_port_avoiding`), the voice LLM's server and its installer refuse a port
  another is set to (`servers.port_clash`), and that server is up only serving its own model
  (`voice.voice_llm_up`), never because something answers on its port.
- **`OPENAI_API_KEY` is optional.** It is the voice only while the voice is OpenAI's
  (`Settings.voice_refusal`, which `serve` asks, refuses each mode for its own missing
  setting); otherwise it buys the voice model's `web_search` (the Responses API — no local
  server has a hosted search tool), which is registered only when there is a key. It never
  reaches a voice LLM (`Settings.voice_llm_endpoint`, by `VOICE_LLM`) that is not OpenAI's
  own host: `VOICE_LLM_API_KEY`, else that key on `api.openai.com` only, else the
  placeholder (`voice_llm_key`; a test is named after it). The endpoint carries the API it
  is asked over (`Endpoint.api`, `responses`), so a second API is a second value.

## Two names

The service is **Keryx**: the package, the command, the directories, the units, the hook,
the repository, and everything the service says in its own voice — the watchdog's alert,
a text, a refusal, the approval broker's note to Claude Code. The **assistant** on the phone
is whatever `ASSISTANT_NAME` says (`keryx/persona.py`): Lyra by default, Jarvis as the
other built-in persona, or any name of the owner's. Three rulings:

- **Messages from the service say Keryx; the assistant's own speech uses its name.** A
  prompt says `{assistant}` wherever the assistant means itself (the voice prompt, the first
  call, the memory update, the subagent suffix). Python wording the voice model hears — a
  tool description, a call-back context — says "you" or "yourself" instead, so no name is
  threaded through it.
- **A persona is a name and a voice.** `PERSONAS` gives each built-in its Realtime voice
  (Lyra `marin`, Jarvis `cedar`), and any other name speaks in `OPENAI_VOICE`, or `marin`.
  `Settings.voice` is what a call uses: on OpenAI, `OPENAI_VOICE` when set, else the
  persona's; on a voice of the owner's own, see "A model is an address". A voice
  the organization may not use is refused with the whole `session.update`, instructions and
  tools included, so a persona's voice is a constant that has been heard, never a guess. A
  coding agent's name is refused as an assistant's.
- **The old name is read, never written, for one release.** `JARVIS_ACTOR=service` is the
  service as much as `KERYX_ACTOR` (a security point: a subagent of either build running the
  other's command must stay the service), and `serve` sets both. `JARVIS_PIN` and
  `JARVIS_CHECKOUT` are read under the new names, a `.jarvis-brief.md` still describes a
  project without a `.keryx-brief.md`, and a `jarvis` command says to run `keryx migrate`. A
  `JARVIS_HOME` is never followed: `serve` refuses until it is renamed. The same holds for a setting renamed later:
  its old name is an alias (`FAKE_AGENTS` → `DEMO_MODE`; `SUBAGENT_MODEL` → `CLAUDE_MODEL`,
  `LLM_SERVER_MODEL`/`_PORT` → `AGENT_SERVER_MODEL`/`_PORT`), `canonical_key` turns it into
  the current one wherever a rule is asked, and the store writes under the current name.
  The old name is read, and kept in step where it is already stored, for one release;
  never created: a write of the setting (or a `lock`/`unlock` of it) gives an old key that
  is there the same value, an unset removes both, and a store without one never gains it,
  so a build running behind still reads the owner's value. `[service_writable]` entries
  are read by the current name, and the current name's wins where both are there.

## Trust has three levels

Caller ID is spoofable, so an inbound number proves nothing — but a call *Keryx placed* is
different in kind, and one bit of trust could not say so. `keryx/trust.py` has the three,
ordered so everything asks for "at least this much":

- **`NONE`** — an inbound call before the PIN.
- **`POSSESSION`** — a call Keryx placed to `Settings.owner_number`. Reaching that phone
  means holding it.
- **`FULL`** — the PIN was given on this call, or the channel is the local microphone.
  `VoiceSession.trusted` is the old spelling of exactly this, and still means it.

Four rulings, and `SECURITY.md` is the threat model:

- **A token is the only thing that may confer possession.** `stream_tokens.outbound_extra`
  records that Keryx placed the call and the number it dialled; `confers_possession` applies
  the rule in `_open_session`, against `Settings.owner_numbers` — never Twilio's `From`/`To`,
  which are the caller's carrier talking. That set is the whole allowlist, because Keryx has
  one owner: `ALLOWED_CALLERS` is the handsets one person picks up, not a guest list, and
  `OWNER_NUMBER` only chooses which one Keryx rings first. Do not narrow it back to the one
  number — that only makes the tier fail silently on the owner's other phone.
- **The PIN is the line between reading and acting, not between private and not.** The
  owner's ruling, and the reasoning is why it is written down: the threat case is somebody
  who has the machine, and they have `secrets.toml` and `KERYX_HOME/pin` — so gating reads buys
  nothing against them. It only ever defended against a phone-side caller-id spoofer, and it
  charged that defence to every ordinary call. So the whole standing briefing comes before
  the PIN (`BRIEFING_BEFORE_PIN`, default on): the digest, the memory, the project names, the
  briefs, the skills. `false` restores the older silence exactly, and the `withheld`
  machinery in `prompts/__init__.py` exists for that — do not delete it. **The ruling
  presumes a PIN exists**, so the predicate is `Settings.reads_before_pin`, never
  `briefing_before_pin` itself: on a machine that has never had one there is no keypad
  entry to trade a read against, and an allowed caller would hear the memory for ever with
  no way to gate it, so everything is withheld until a PIN exists (see "Working
  agreements"). `announce(text,
  needs=…)` says which kind each announcement is: news needs nothing, an approval needs a
  call that could answer it. `Announced.delivered` still takes `POSSESSION`, because a
  stranger hearing the news is not the owner having been told, so the text and the call-back
  still go out.
- **The read-only tools over that same material follow it.** `list_projects`, `list_tasks`,
  `get_task_status` and `get_task_result` are the caller asking for a bit of the briefing out
  loud, and gating them while the prompt states the same facts is incoherent; `read_gate` is
  the gate, and it follows `BRIEFING_BEFORE_PIN` so that setting has no hole in it.
  **`recall` is not one of them and stays at `FULL`** — the briefing is a bounded, curated
  context the owner can read with `keryx memory` and prune, and it is the same whatever the
  caller says, where `recall` is an unbounded, caller-steered query over every raw transcript
  Keryx has ever written. That is a different quantity of exposure, and the one thing on the
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
single most important invariant here: hearing what Keryx believes is recoverable, editing it
is not. A spoken PIN is `[PIN]` in every transcript line
(`continuity.transcripts.redact_pin`), and everything that hands a transcript to a model
redacts again, for logs written before. Do not narrow the subagent's tools instead.

## Continuity is three pieces

A realtime session starts blank — the provider keeps nothing across sockets — so what
Keryx knows at the top of a call is assembled every time by
`keryx/continuity/briefing.py`:

- **The digest.** `Task.reported_at` is the only record that Keryx *told the owner*; `announced`
  and `sms_sent` only say a delivery was attempted, and neither survives a call they missed.
  Until `reported_at` is stamped, the task rides at the top of the next call — from the
  greeting, PIN or no PIN, along with the rest of the standing briefing
  (`reads_before_pin`; see "Trust has three levels" — a machine with no PIN at all is
  handed none of it). Exactly one
  thing stamps it: the voice model's `mark_reported` tool, after it has spoken the result.
  Do not stamp it from a delivery path — hearing something twice is recoverable, never
  hearing it is not.
- **The memory.** `keryx/continuity/memory.py` owns `data_dir/memory.md` outright — the
  file API *and* the writer. It subscribes to `SessionEnded` and, for an authorized call only,
  dispatches a subagent (`prompts/memory_update.md`) that folds the call into the file; the
  next call reads it back through `briefing`. Its headings are nested one level when
  embedded, so its sections cannot be mistaken for instructions. `memory_skeleton(owner)` is
  the only place its sections are written down — the update prompt renders it, and
  `seed_memory` (behind `keryx setup` and `keryx memory seed`) fills it, and
  `add_standing_facts` adds to it; never restate the structure elsewhere.
  Its *absence* is the marker of a first call: a trusted session with no memory renders
  `prompts/first_call.md` in place of it and opens as a short introduction instead of an
  ordinary call. That is the only record of "has been onboarded" — do not add a second one,
  and do not have the session write `memory.md` itself; the interview's last turn says the
  facts out loud, and the updater folds them in like any other call's.
- **`recall`.** `keryx/continuity/recall.py` searches past transcripts and past task
  summaries on demand. Matching stays literal on purpose: the query is speech that
  transcription has already mangled once, and a fuzzy hit gets read out as if it were fact.
  It needs the PIN, redacts it, and skips calls that never gave it — the one read that did
  not move when the briefing did, because it is unbounded and the caller steers it.

`Task.internal` marks work Keryx asked for itself (today: the memory update). It hides the
task from the spoken lists, the digest, `recall`, the daily cap and the notifier — and
restricts *nothing* about the subagent. It is not a task kind; do not grow it into one.

`Task.needs_restart` is the other flag, and it is a *request*, not a fact: the subagent says
`RESTART_REQUIRED: <why>` above its `SPOKEN_SUMMARY:` because it is the only thing that knows
it edited `src/keryx/**` (`git describe --dirty` flips on any open edit). Honoured only on a
task that succeeded, never on an internal one. The Notifier then hands that task's call-back
to the restart's confirmation, which carries both halves — what the work came to, and whether
it is running. Do not make a subagent restart Keryx itself; it is inside the cgroup.

## Settings live in a store, and the service may change only some

`keryx setup`, `keryx config` and `keryx auth` replaced a hand-edited `.env`. A plugin's
settings are the exception: every one but its secret lives in `DATA_DIR/tools/<name>.toml`
beside it, written only by `keryx.plugins` (validated, then quoted, so no value can add a
key), and so out of `set_config`'s reach — the budget and the email model included, an
accepted cost. Its secrets (`SLACK_BOT_TOKEN`, `OPENAI_ADMIN_KEY`, `ANTHROPIC_ADMIN_KEY`)
stay protected in `secrets.toml`. A setting a plugin replaced (`plugins.RETIRED_KEYS`) is
ignored where it is left, named by `doctor`, and moved by `keryx plugins install
--from-settings`, which only `ConfigStore.drop_retired` then removes. Four rulings:

- **Secrets in a 0600 file, not a keyring.** Plain settings in `KERYX_HOME/config.toml`,
  every `repr=False` field in `secrets.toml`, both 0600 in an 0700 directory and replaced
  atomically (`files.write_private`). A keyring cannot be unlocked by a headless systemd
  unit, and the split is the one Claude Code and Codex make. Precedence is code → process
  environment → `secrets.toml` → `config.toml` → default, and nothing is read from the
  working directory (see "Storage follows XDG"). `KERYX_HOME` is an environment variable
  only, since it is what says where the settings are.
- **A secret never on argv.** `keryx config set` refuses one given as a value; it takes
  `--stdin` or `--from-env`. `config get` and `config list` never print one, and a refused
  value is never quoted back (`hide_input_in_errors`).
- **Two actors.** The owner at a terminal may write anything but the PIN. The *service* —
  the voice model's `set_config` (behind `pin_gate`), or any subagent, since `keryx serve`
  sets `KERYX_ACTOR=service` for everything it starts — may write only a service-writable
  key: the field's default, overridden by `keryx config lock|unlock` under
  `[service_writable]`. `PROTECTED_KEYS` (every secret, the PIN, trust, approvals, publishing
  issues, spending, deletion, the network, the debug switches) can never be unlocked, and a hand edit that
  tries is ignored and reported by `doctor`. The service may tune a limit in
  `permissions.NEVER_OFF` but never set it to 0 ("no limit"), and every write is checked
  against the whole store, so nothing it saves can stop `keryx serve` from starting. The
  commands that write what the service may not — `config import-env`, `auth login|logout`,
  `memory seed`, `setup`, `setup mark`, `setup profile --apply`, `setup project-context
  --explore|--folder|--apply`, `pin set`, `config lock|unlock`, `migrate`,
  `plugins install|remove|set`, `models use`, `voice use` (before any install, handshake or
  save), `voice download`, `models start|stop`, `service install|uninstall`, `phone
  webhooks --apply` (before Twilio is asked anything) — refuse
  outright under
  `KERYX_ACTOR=service`, and the tasks setup itself dispatches (the smoke test, project
  context) run as the service. This binds Keryx's own tools; it is not a
  sandbox (SECURITY.md). A new field decides its `service_writable` on purpose, and
  `tests/config/test_permissions.py` names the writable set.
- **The PIN is not a setting.** It stays in `KERYX_HOME/pin`, a write-once file of its own
  and never a key in `config.toml`; `config set KERYX_PIN` is refused, `import-env` (and
  `migrate`, through it) moves a `.env` PIN there — and refuses the whole import when a
  different PIN is already enrolled, because silently switching would lock the owner out.

`keryx setup` reads `doctor`'s checks to decide what is left (each has a `section`, and is
`missing` or `failed`), walks only that, and remembers what it has walked
(`[setup] walked`) so an optional section left for later is not asked about on every run.
It never assumes what the machine lacks: an agent that is signed in is not asked how to pay.
The one write it makes outside the machine — the Twilio webhooks — comes after showing both
addresses and a yes, through `keryx/phone.py`, which `keryx phone webhooks --apply` calls
after the Mac app has shown the same two addresses and had the same yes. `keryx setup plan` is the same decision for the Mac app
(`setup/plan.py` over `wizard.section_statuses`), so the two cannot drift: decide a section's
status in the wizard, and the plan follows. `walked` there only ever settles an optional step,
or `pin` (the owner's "let the first call set it"); a required step is done by its checks,
and `setup mark` refuses one. A soft warning never holds a step or `ready` back, a hard
failure no step owns is the service's (no hard failure beside `ready: true`), and the plan
always answers — exit 0, a document — whatever the settings.

## Storage follows XDG

Keryx keeps its files where uv, gh, git and neovim keep theirs, on Linux and macOS alike —
never `~/Library`, so no `platformdirs` — and each `XDG_*_HOME` is honoured, an empty or
relative one ignored as the specification says (`config/files.py::xdg_home`):

- `~/.config/keryx` — `KERYX_HOME`: `config.toml`, `secrets.toml`, `pin`, the Google
  client file. Only the environment moves it.
- `~/.local/share/keryx` — `DATA_DIR`: `tasks.db`, `tasks/`, `calls/`, `memory.md`,
  `projects/`, `workspace/`, `tools/` (the owner's own voice tools and the plugins), the sign-in tokens,
  `codex/`, and `pin-failures.json`.
- `~/.local/state/keryx` — `STATE_DIR`: `logs/`, `restart.json`, the version stamps,
  `approvals/` and `approvals.sock` together, so the hook needs one directory.
- `~/.cache/keryx` — `CACHE_DIR`: what can be downloaded again — the local models
  (`models/`), a llama.cpp build Keryx installed (`llama.cpp/`), and on `feat/local-wakeword`
  the wake-word models.

Four rulings:

- **The PIN is in the configuration directory, not the data one.** Where the PIN is must not
  depend on a setting: at `DATA_DIR/pin`, a `DATA_DIR` pointed at an empty directory found
  no PIN, and no PIN is an open enrolment door. `pin-failures.json` stays with the data,
  not the state, because people treat state as disposable and losing it hands a guesser a
  fresh budget.
- **Nothing is read from the working directory.** No `.env`, no `.secrets/`. `serve` (and
  every command that reads the data) refuses while one is there, while `~/.jarvis` still
  holds its files, or while one of Jarvis's XDG directories still holds a file
  (`Settings.storage_refusal`). The signal is the old files being
  there, never the new directory missing — `ensure_dirs` makes that on any command.
- **`keryx migrate` plans before it touches anything, and can run twice.** A conflict
  stops it before the service is stopped; an entry already moved is not in the next plan.
  Nothing is deleted but a stale socket: `~/.jarvis` is renamed aside with its leftovers,
  and each of Jarvis's XDG directories moves whole — whole, because what is in them has
  outgrown any list of names. Claude sessions that ran in the old workspace are let go, so a
  follow-up starts afresh, and the imports of the tools in `DATA_DIR/tools` are rewritten
  (their import lines only). `CLOUDFLARE_TUNNEL` is pinned to `jarvis` when it was the
  default, and Jarvis's unit is retired before Keryx's is installed, so two tunnels never
  run at once. Running twice covers the units too: `STATE_DIR/migrate-units-pending.json`
  (`config.migrate.UNITS_PENDING`, 0600, written atomically) goes down before the first move
  — inside Jarvis's state directory when that is about to move onto `STATE_DIR` whole, so it
  is carried there — and is removed only once Jarvis's units are retired and Keryx's and the
  approval hook re-rendered with nothing failing (`InstallFailed`). A unit counts as retired
  by what is left — not known to its manager, its file gone — never by its command's exit
  code, which is non-zero for one a run before already took away; the restart's "a negative
  exit is the restart working" has no part here, since `migrate` never runs inside a unit.
  The marker records the unit being installed and whether the hook is to be re-rendered,
  because a hook installer killed part way can leave Claude's settings no longer naming it.
  While it is there the plan is not empty: it says why, the run re-renders, and the unit and
  the hook it names are rendered even when nothing is left on disk to find them. The
  service is stopped before the marker goes down, which is fine: the run's own start, or
  the owner, undoes a stop. Without it a run killed after the moves, or an
  installer that failed, would leave the units naming the old paths with nothing left to do.
- **The service resolves what its installer's terminal resolved.** The units render
  `KERYX_HOME` and the four `XDG_*_HOME`, and the restart watchdog's transient unit is
  handed them with `--setenv`: a user manager's environment is not the service's. A
  relative `DATA_DIR`, `STATE_DIR` or `CACHE_DIR` is refused for the same reason.

## Restarts are three halves

The process that runs `systemctl restart` is the one that gets killed, so
`keryx/restart/coordinator.py` splits the flow across that death and joins it with
`state_dir/restart.json`: `request()` writes the record and hands over, `resume()` (one task
per `keryx serve`) finds it on the far side and rings back with a status summary. Neither
half may interrupt a call — a restart asked for during one waits for the line to clear, and
the confirmation is announced or texted rather than dialled into a live session. Keep it
that way, and keep every failure path landing somewhere a human can find it
(`keryx restart --status`). Whether a restart may be attempted at all is a fact about the
*process*: `SERVICE_MANAGER=auto` resolves from its own cgroup (systemd) or
`XPC_SERVICE_NAME` (launchd), never from `systemctl` being on PATH, so a hand-started copy
refuses rather than restart the installed one. Only `keryx restart` and `doctor`, which run
outside the unit, ask whether it is installed.

"Did it load the change" is answered from `state_dir/running-version`, stamped by `mark_running()`
at the top of `keryx serve` — *not* from `current_version()` at request time. The checkout moves
under a running process, and the normal order (edit, commit, ask for the restart) puts the new
commit on disk before the question is put, so a request-time read compares the new commit with
itself and reports that nothing loaded. Process start is the only moment the checkout and the
running code are the same thing.

The third half is `keryx/restart/watchdog.py`, and it exists because the first two both live
*inside* Keryx. A restart is usually loading a change Keryx just made to its own code; a
change that will not import means there is no new process, so nothing runs `resume()` and
nobody is told anything — silence that reads exactly like success. So `_execute()` arms
`keryx restart-watch` in a transient `systemd-run --user` unit *just before* handing over
(a restart signals the whole cgroup; anything we merely fork dies with us), and it acts only
on the case neither other half can see: a record still `pending` at the deadline. It alerts
by text plus a plain `<Say>` call — never `<Connect><Stream>`, whose media stream is served
by the process that is not running.

Two rulings that look like bugs if you do not know them. A **negative** exit code from the
restart command is the restart working: `systemctl` is inside the cgroup it tears down, so it
is killed handing over and returns `-15`. And "back up" is not "working" —
`keryx/restart/logscan.py` scopes the service's log files by byte offset (`marks()` before,
`errors_since()` after) so the confirmation can say what broke, and those errors are spoken
*before* the housekeeping.

## The approval bridge runs the other way

Everything else in Keryx carries a result *outwards* from work the owner asked for.
`keryx/approvals/` is the opposite: a Claude Code session on their own screen has stopped
and asked *them* something, and they are not at the keyboard. A hook in `~/.claude/hooks/`
(canonical copy: `scripts/claude_hooks/keryx_approval.py`, installed by
`scripts/install-claude-hook.sh`) hands the pending prompt to the broker over a Unix socket
and blocks; five minutes later, if they still have not answered, Keryx rings them.

Four rulings hold it up, and none of them is a preference:

- **A Unix socket, never an HTTP route.** `cloudflared` puts the whole of port 8080 on the
  internet. `state_dir/approvals.sock` at 0600 is unreachable through it by construction.
- **`policy.py` is the *primary* control, not a second layer.** A `PermissionRequest` hook
  returning `allow` appears to skip the CLI's own `permissions.deny` re-check, so whatever
  `classify` calls eligible is what a keypad digit can run. It is an allowlist, it starts
  small, and the denylist wins over it. Do not widen it without saying why in the commit.
  It decides on exactly what runs: the raw command's argv (never a normalised copy, never
  a request the hook had to trim), read back whole or not at all.
- **The keypad decides, never the transcription.** `answer_approval` cannot answer
  anything; the most it does is put a menu in the model's mouth. `ApprovalBroker.digit` is
  the only thing in Keryx that can approve a tool call, it is reachable only from a call
  that has proved something — `FULL`, or the `POSSESSION` of a call Keryx placed to the
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
escalation (settling a request cancels its sleeping timer), and pending is re-read before
dialling and again before any verdict is applied. A `PostToolUse` is matched on
`models.resolution_digest`, not the whole input, because the CLI hands the question tools
back reshaped (`AskUserQuestion` with its answers added, `ExitPlanMode` as `{}`); matching
the whole input rang the owner about every question they had already answered (#56). A
rejection at the keyboard needs none of this: the CLI kills the hook, and the broker reads
EOF. What it cannot see is a tool approved at the keyboard that is still running at the
deadline: the CLI says nothing until it finishes, and the `settled` line's `escalation` field
is how that shows up.
A prompt that arrives while they are already on a call that could answer it is announced into
that call rather than ringing them a second time. `uv run keryx approvals` is the audit trail and
`--disable` is the kill switch, which is a file so it works without a restart.

## Billing reads, and only reads

`keryx/integrations/billing.py` answers "what am I spending" from the provider's own billing API,
behind the `check_billing` plugin (`plugins/billing.py`), whose default provider, budget and
scoping ids are in `check_billing.toml`; the admin keys are read from the store when it is
asked, so one saved since startup needs no restart. Four rulings, and the first two are the
ones that bite:

- **Anthropic's amounts are decimal strings in cents.** `"123.45"` USD is `$1.2345`.
  Divide by a hundred; there is a test named after it. OpenAI's `amount.value` is a float
  in dollars. The two providers do not agree, and a hundred-fold error read out loud as
  money is the worst thing this feature can do.
- **It needs an admin key, not the agent's key.** `OPENAI_API_KEY` gets a 401 on
  `/v1/organization/costs`; `OPENAI_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY` are separate
  settings. With neither set we still *try* the ordinary key and report the 401, because
  a clear "that needs an admin key" beats a tool that is silently not registered.
- **`GET` and nothing else.** `_get` takes no body and no method, so no caller can turn it
  into a write. Keep it that way, and keep the plugin `needs_pin=False`: it is the one capability
  in Keryx that cannot change anything, and asking what a number is should not need a PIN.
- **The spend figure is not per-key, and says so.** OpenAI's costs endpoint filters by
  `project_ids` and nothing finer; `BillingReport.scope` carries what the number actually
  covers. Token *usage* can be narrowed to a key id. Do not let the two blur.

Nothing reaches the caller but the report: `as_dict()` carries no credential, `classify`
reduces a failure to its status code (a 401 body can quote the key back), and the only
place a key appears at all is `redact()` in a log line. Every failure is a `status` plus a
sentence written to be spoken, never a raised exception.

## Cluster stats read, and only read

`keryx/integrations/cluster.py` answers "what's free on the cluster" and "am I still running"
from Slurm, behind the `cluster_stats` plugin (`plugins/cluster.py`). Nothing knows a
cluster until the owner turns it on: its hosts are `[clusters]` in `cluster_stats.toml`
(alias → partition), an empty table is refused, and the wizard and `keryx plugins hosts`
propose them from `~/.ssh/config`. Never hardcode a cluster. Three rulings, and the first is
the one with a scar behind it:

- **Never our own connection to the cluster.** Where auth is 2FA behind an ssh
  ControlMaster, no non-interactive process can answer the second factor: a direct attempt
  against a dead master *hangs*, and a storm of those retries is how an address gets banned
  by the login nodes. The built-in guard is the guard (`ControlMasterSsh`): `ssh -O check`
  on the local control socket before anything — no network, no auth attempt — and the read
  only over that live master, in `BatchMode`; a dead master, or one that died mid-read, is
  `auth_expired`, terminal, never retried. A host with no ControlMaster in `~/.ssh/config`
  is never offered. A guard script of the owner's own (`guard` in the TOML, run by
  `GuardedSsh`, exit `42` for expired) is still honoured, but nobody needs to write one; a
  guard gone missing is refused, never a fallback that dials out by itself.
  `CLUSTER_SSH_NO_NOTIFY=1` is set because a guard script may notify the owner some other
  way, and they are on the phone, which is where the sentence belongs.
- **Read-only by construction.** `build_script` assembles the remote command from module
  constants and refuses any command whose first word is not in `READ_ONLY` (`squeue`,
  `sinfo`); there is a test named after it. The only thing the model chooses is a cluster
  *name*, looked up in the configured set and refused when it is not there — no string
  from the model reaches a shell, and `plugins.clean` refuses a name or partition that is
  not a bare word before either is written. Un-PIN-gated for the same reason as `check_billing`, and the payload is counts
  plus the owner's own job ids: no job name, no path, no other user.
- **Idle, planned and down are three numbers, not one.** `sinfo` without `-N` aggregates by
  state line and its totals are silently wrong; a `planned` node is backfill holding
  hardware for a queued job, not a free one; and most pending jobs are blocked on a
  dependency rather than competing for GPUs. The fixtures in
  `tests/integrations/test_cluster.py` are synthetic but preserve the shapes of real cluster
  output, and the totals asserted on are worked out by hand from their rows. Do not collapse
  them to be brief.

## Issue reports go out, and only the pattern goes with them

"That's a bug, report it" and "suggest that Keryx could…" are dispatches like any other;
there is no voice tool and no task kind for them. `keryx/issues.py::IssueReporting`
resolves where they go (`ISSUE_REPO`, the upstream repository by default) and what the
subagent may read (`Settings.checkout` — `KERYX_CHECKOUT`, else the checkout the code runs
from — its logs, and the call it came from), and `prompts/subagent_issues.md` points every
subagent at `skills/keryx-report-issue`, read from that checkout. The voice prompt's
paragraph names feature requests as well as bugs, because a model left to itself files the
bug and hears the wish as chat. It also says most wishes are better built now, as a tool of
the owner's own, and has the voice ask once when the owner's words don't say which they
want: only they know whether they meant "build it" or "pass it on". Four rulings:

- **Off until the owner turns it on by hand**, because it publishes. `ISSUE_*` is in
  `PROTECTED_KEYS`: neither the service nor a subagent may set `ISSUE_REPORTING` or
  `ISSUE_REPO`, nothing unlocks them, and the setup agent hands the question to
  `keryx setup` rather than answering it. The wizard's
  Issue reports section counts as left until it has been walked, whatever `doctor` says —
  off passes `doctor` — and it checks `gh` (`issues.gh_status`, a `Probes` field) and offers
  `gh auth login` on the terminal. Nothing else in Keryx signs `gh` in.
- **The owner does not pay for Keryx's bugs.** The look is about ten tool calls: no tests,
  no reproduction, no fix, no feature built, no edit to the checkout. The issue says the
  look was brief.
- **The repository is public.** No number, PIN, secret, name or project of the owner's, and
  nothing quoted from a call, a task or the memory — the pattern, never the words. A
  security problem is never filed there at all; it is pointed at the private advisory.
- **A filing that fails lands in the report**, never in a retry loop or another channel.

## Platforms

`main` runs the phone channel, on macOS and Linux alike. The local wake-word channel lives on
`feat/local-wakeword` until it is ready: it is macOS-only, because openwakeword needs
`tflite-runtime`, which has no cp312 wheel. The `local` session channel and its `FULL`
trust stay on `main`, because `keryx loopback` (the WAV harness) runs through it.

The coding agents are optional the same way, by choice rather than platform: each SDK is an
extra (`claude`, `codex`, `all`), because each bundles a CLI of hundreds of megabytes. So
`claude_agent_sdk` and `openai_codex` are imported only where an agent runs, never at module
scope, and an agent whose package is missing is `registry.installed() == False`: shown as
not installed with its `uv sync --extra` command, never offered, refused as `AGENT_BACKEND`
by `Settings.agent_refusal`. `tests/agents/conftest.py` skips a backend's own tests without
its SDK, and a test that is not about installation asks for `every_agent_installed`.

The Mac app (`macos/`) is macOS-only, by nature rather than choice: SwiftUI and AppKit, built
with SwiftPM for macOS 14 and later. The Command Line Tools are enough, so nothing in its
build or tests needs Xcode, and CI builds and tests it on `macos-latest` beside the Python
legs.

## Testing rule

No network or hardware access in tests. OpenAI, Twilio, the Claude Agent SDK and the
Codex SDK are always accessed through an
injectable interface (a `Protocol`) with a fake/test double used in tests —
never the real network or hardware, and never a real `codex` process: the Codex
tests replay `tests/agents/fixtures/codex_app_*.jsonl`, recorded from the bundled
app-server, into the SDK's own typed models. Heavy imports (an agent's SDK) are guarded
inside functions, not imported at module scope, so the suite runs without them.

A directory's `conftest.py` must hold however a command line orders the files: pytest 9.1
gives its fixtures to the first collector made for that directory only, so `tests/plugins/a.py
tests/b.py tests/plugins/c.py` handed `c.py` the root `settings`. `tests/repeated_directories.py`
(turned on in `tests/conftest.py`) registers them on every later one; keep it until pytest does.

## Working agreements

- Only `config/store.py` writes the configuration, and a secret is never on argv, in a log
  or in output. Never print or paste `secrets.toml` or an old `.env`. `docs/configuration.md`
  is generated from `Settings` (`python -m keryx.config.reference`); a new field gets a
  description, a group, a title, and whether it is advanced, and `tests/test_docs_sync.py`
  fails until the doc is regenerated.
- The database runs ahead of the code. `_migrate` upgrades `tasks.db` from whichever process
  opens it first, and `keryx serve` holds the `Task` it imported at startup, so a new column
  reaches the file while the service is still a build behind. `Task.from_row` drops columns it
  has no field for; keep it that way, and keep writes naming their columns so the older build
  cannot blank the newer one's data.
- Keryx does not text. `SMS_ENABLED` is off by default (many accounts lack SMS permission
  for their region, and Slack is the written channel), so gate any send on
  `TwilioOut.can_text` and never on `configured` — outbound *calls* are unaffected, and the
  restart watchdog's `<Say>` alert is the last thing working when Keryx is down.
- **One action is one sentence.** The wording the model is handed — the tool descriptions,
  the `*_MESSAGE` constants in `builtin_common`, the call-back contexts, the voice prompt —
  says what *not* to say as firmly as what to say, because the failure mode is never
  silence, it is a second turn restating the first. Transcripts of real calls are in
  `~/.local/share/keryx/calls/`; read a few before editing any of it. They may contain a
  spoken PIN and other personal details, so nothing from them is ever copied into code,
  tests, docs or commit messages — describe the pattern, never quote the call.
- The interface names `tasks/agent_runner.py` re-exports, and the `list` methods on the
  task store and manager, keep their names and signatures (extra optional keyword
  arguments are fine). The rulings in this file bind: follow them, and amend this file in
  the same commit when one changes.
- Conventional commits (`feat:`/`fix:`/`chore:`/`docs:`) with the Co-Authored-By
  Claude trailer. A subagent Keryx dispatched adds `Keryx-Task: <id>` as well (it was
  `Jarvis-Task:` before the rename), so `git log -E --grep '^(Jarvis|Keryx)-Task:'` is
  everything the owner asked for out loud rather than typed — the one thing `git log`
  cannot otherwise recover.
- Clean and minimal over clever; TDD, with `uv run pytest -q` and
  `uv run ruff check src tests` pristine before a commit. Coverage has a floor (96%) and it
  is a ratchet: raise it when the measured number moves up, never lower it to pass.
- All four of Keryx's directories are 0700 and the files under them 0600
  (`config.secure_dir` / `secure_file` / `write_private`). Anything new that writes there
  goes through them.
- A configured `KERYX_PIN` is 6-8 digits and `keryx serve` refuses to start otherwise.
  With none set anywhere, **the first call may enrol one** and that is the only way the
  phone ever sets a PIN: `Settings.pin` resolves environment-then-`KERYX_HOME/pin`,
  `pin_enrolment_open` is the door, and `config.write_enrolled_pin` shuts it with
  `O_CREAT | O_EXCL` — the kernel refusing a second write is the whole guarantee, which is
  why no voice tool, no call and nothing the service starts reaches a setter, and why you
  must not add one that could. There are two keyboard paths and one set of doors
  (`keryx/setup/pin.py`): `keryx setup` at a terminal, which sets a PIN the same way when
  there is none and replaces one only after two explicit yeses, with the new digits already
  typed twice; and `keryx pin set --stdin [--replace]`, the owner's Mac app at the keyboard
  (the owner's ruling of 2026-10-07), which takes the digits twice on stdin, compares them in
  constant time before judging them, and replaces only with `--replace`. Both replace by an
  atomic rename (`pin.replace_pin_at_keyboard`) so no failure leaves no PIN, both are
  owner-only — refused under `KERYX_ACTOR=service` before a digit is read — and neither says
  the digits or their length anywhere. A `KERYX_PIN` in the environment wins, so `pin set`
  refuses rather than write a PIN nothing would read.
  A running service keeps the PIN it started with until it restarts, and both say so. The
  digits are keyed twice and compared (`session._enrol_keypad_pin`), never spoken: a
  mishearing here is unfixable. Any `KERYX_HOME/pin` shuts the door, usable or not, and so
  does a PIN still in an unmigrated `~/.jarvis`; only the owner at the keyboard re-opens it.
  Until a PIN exists nothing of theirs is read out (`reads_before_pin`), and SECURITY.md
  carries the accepted risk.
  Wrong PINs also count across calls (`keryx/pin_guard.py`): while that has PIN entry locked
  the right PIN is refused before it is compared, and nothing resets the count early — not
  the lock lifting, not a right PIN. That a spoofed caller can keep the owner's PIN locked is the
  accepted price (SECURITY.md); do not buy it back with a reset-on-success or a per-caller
  count, both of which hand a guesser a fresh budget. What is not a PIN at all is not a
  wrong one: a spoken PIN is read back to its digits (`config.spoken_digits`: separators and
  digit words only), and anything that is not 6-8 digits after that is `incomplete`,
  compared with nothing and counted nowhere, since it cannot match. Do not grow
  `spoken_digits` into picking digits out of a sentence — a stray word makes it no PIN.
  `serve` also refuses the phone channel
  with `DEBUG_SKIP_TWILIO_VALIDATION` on behind a `PUBLIC_HOST`.

## Reference docs

The original design spec and implementation plan are the owner's notes, kept out of git in
`.superpowers/` (`specs/2026-08-18-jarvis-voice-agent-design.md`,
`plans/2026-08-18-jarvis-voice-agent-plan.md`). A fresh clone does not have them. Where they
are present they are background; this file and `SECURITY.md` are what binds.
