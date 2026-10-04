# Coding agents: Claude Code and Codex

Keryx does the talking; a coding agent on your machine does the work. Two are supported,
and a task runs on whichever one it was handed to for its whole life:

- **Claude Code** (`claude`), driven through the Claude Agent SDK. The default.
- **Codex** (`codex`), driven through OpenAI's `openai-codex` Python SDK, which runs the
  `codex` CLI it bundles as an app-server.

Each agent is an optional extra of the same name — `claude`, `codex`, or `all` — and each
SDK carries its own CLI, so there is nothing else to install. They are large: a complete
environment measured about 760 MB, 510 MB with Codex alone, 410 MB with Claude alone and
160 MB with neither (dev tools included in all four). The Codex SDK is pinned to one exact
version, because Keryx reads its messages field by field.

| Command | Installs |
|---|---|
| `uv sync` (in a clone) | both agents — the default `agents` group is `keryx[all]` |
| `uv sync --no-group agents --extra codex` | Codex only (`--extra claude`: Claude only) |
| `uv sync --no-group agents` | neither: `keryx serve --demo` only |
| `uv sync --extra codex` | both still: the default group comes along |
| `pip install '.[all]'`, `'.[codex]'`, `'.[claude]'` | what it names; plain `pip install .` installs neither |

Python packaging has no default extras (PEP 771 is a draft, and uv 0.11 has no
`default-extras`), which is why the default is a dependency group. One consequence: `uv run`
syncs the environment back to the defaults before it runs, so on a narrowed checkout give it
the same flags (`uv run --no-group agents --extra codex keryx …`) or set `UV_NO_SYNC=1` —
the service units run `uv run`, and would otherwise put every agent back.

An agent that is not installed says so in `doctor` and `keryx setup`, with the command that
installs it; it is never offered to the voice model; and `keryx serve` refuses to start with
it as `AGENT_BACKEND`. `keryx serve --demo`, which answers every task with a sample reply,
needs neither.

`AGENT_BACKEND` picks the one that does the work when you do not say, and `AGENTS_ENABLED`
lists every one a task may be sent to. With more than one enabled and signed in, you can
name it out loud — "have Codex look at the build" — and a model name picks its agent too
("use opus" is Claude, "use terra" is Codex). A follow-up always goes back to the agent that
started the task: a session belongs to the agent that issued it.

`uv run keryx setup` is the way in: its "Coding agents" section shows what is installed and
signed in, asks which agents and which is the default, runs the sign-in each one is missing
(and leaves one that can already run alone), and runs one real task through each as a smoke
test. From the command line: `keryx auth login claude|codex`, `keryx config set
AGENT_BACKEND …`, and `keryx auth status --smoke`. `uv run keryx doctor` checks the same
things on every run.

## Signing in

Both agents take the same three tiers, in the same order — the first one set wins:

| Tier | Claude | Codex |
|---|---|---|
| API key (pay per token) | `ANTHROPIC_API_KEY` | `CODEX_API_KEY` |
| Headless subscription token | `CLAUDE_CODE_OAUTH_TOKEN`, from `claude setup-token` | `CODEX_ACCESS_TOKEN` (see below) |
| Stored subscription login | `claude`, then `/login` | `codex login`, or `codex login --device-auth` with no browser |

The credential reaches the agent in its environment and nowhere else — never on its command
line, never in a log. `OPENAI_API_KEY`, the voice model's key, is never handed to Codex:
that would quietly move a ChatGPT-plan user onto per-token billing. Set `CODEX_API_KEY` if
that is what you want.

Codex's own process is handed a copy of Keryx's environment, so Keryx overrides every
credential variable the chosen tier does not use with an empty value, which Codex treats as
unset. Beyond that, each Codex tier goes in its own way, because of what the app-server the
SDK runs actually reads (checked against 0.157.1):

- **`CODEX_API_KEY`** is *ignored* in the app-server's environment. So Keryx logs in with
  the key once — on stdin, never on a command line — into a Codex home of its own
  (`~/.local/share/keryx/codex`, owner-only, with your `config.toml`, `AGENTS.md` and
  `skills` linked in, but not `hooks.json`: your hooks are your own automation), and logs in
  again only when the key changes. Your own `~/.codex` login is never touched.
- **`CODEX_ACCESS_TOKEN`** *is* read from the environment, so that is all Keryx does with
  it: nothing is stored. It is not a ChatGPT token but an OpenAI *agent identity* token; a
  bogus one fails cleanly, but no real one has been run yet.
- **The stored login** is your own `~/.codex`, exactly as the `codex` CLI uses it.
  `keryx auth login codex` runs the SDK's bundled `codex login`, which shares that home with any
  `codex` you have on PATH.

## What each agent can do here

Every ✅ was checked against the real CLI while the feature was built — the Slack and Gmail
rows as far as the mechanism: an MCP server handed to Codex with its secrets passed by name,
proved with a stand-in server rather than Slack or Google themselves. Anything that was not
checked is marked, and listed under "possible".

<!-- agents:start -->
| Capability | claude | codex | local |
|---|:---:|:---:|:---:|
| Dispatch by voice, and name the agent out loud | ✅ | ✅ | ✅ "the local model" |
| Follow-ups resume the same session | ✅ | ✅ `thread_resume` | ✅ as its harness |
| A follow-up reaches a task while it is still running | re-runs after the turn | ✅ into the running turn | as its harness |
| Cancel stops the work | ✅ | ✅ `turn/interrupt`, then the app-server is stopped | ✅ |
| Progress lines in `DATA_DIR/tasks/<id>.log` | ✅ | ✅ | ✅ |
| `SPOKEN_SUMMARY:` / `RESTART_REQUIRED:` | ✅ | ✅ | 🟡 as well as the model follows the instruction |
| Project working directory and briefs | ✅ | ✅ | ✅ |
| The per-call memory update | ✅ | ✅ when it is the default | ✅ when it is the default, and then it stays on your machine |
| API key and stored subscription login | ✅ | ✅ | — its server's own optional key (`LOCAL_AGENT_API_KEY`) |
| Headless subscription token | ✅ | 🟡 read from the environment; no real token run yet | — |
| Choosing the model by name | ✅ opus, sonnet, fable, haiku | ✅ astra, sol, luna, terra | — the one `LOCAL_AGENT_MODEL` names |
| Wall-clock cap (`SUBAGENT_TIMEOUT_S`) | ✅ | ✅ | ✅ |
| Turn cap and dollar cap | ✅ `SUBAGENT_MAX_TURNS`, `SUBAGENT_MAX_BUDGET_USD` | — | turn cap in Claude Code; no dollar cap |
| Tokens recorded on the task (`keryx tasks show`, per project in `keryx tasks usage`) | ✅ | ✅ | ✅ as the server reports them |
| Dollar cost recorded on the task | ✅ | — a plan call has no price | — a local model has none |
| Slack, through the server the `send_to_slack` plugin names (`mcp_server`) | ✅ | ✅ handed over from `~/.claude.json` | as its harness |
| Gmail and Calendar | ✅ claude.ai connectors | ✅ with `GOOGLE_WORKSPACE_MCP=true` | with `GOOGLE_WORKSPACE_MCP=true` |
| Its own instructions file | `~/.claude/CLAUDE.md` | `~/.codex/AGENTS.md` | its harness's |
| Its own skills, listed to the voice model | `SKILLS_DIR` | `~/.codex/skills` | its harness's |
| Nothing to install beyond `uv sync` (extra `claude` / `codex`) | ✅ | ✅ bundled CLI (~350 MB) | a model server: `keryx setup` installs one |
| `doctor`, `keryx setup`, `keryx auth`, `--demo` | ✅ | ✅ | ✅ (no sign-in: it has an address) |
| Approval bridge for your on-screen sessions | ✅ | — | — |
<!-- agents:end -->

Two of those are worth knowing before you switch:

- **Gmail and Calendar.** Claude gets them from its claude.ai connectors, which live on the
  Anthropic account. Codex has none, so with Codex enabled, connect Google for agents in
  `uv run keryx setup` (or `keryx auth login google-workspace`); `doctor` warns until you
  have.
- **The approval bridge** (`keryx approvals`) is for Claude Code sessions on your own
  screen that stop and ask you something. It has nothing to do with which agent Keryx
  dispatches to, and it stays Claude Code only.

Both agents run with approvals and the sandbox off (`bypassPermissions` for Claude, the
full-access sandbox with approvals never asked for Codex). The phone PIN gates the dispatch,
the same way whichever agent runs it: see [SECURITY.md](../SECURITY.md).

Two more that are true of both, and worth knowing:

- **A follow-up to Claude while it works waits for the turn to end**, then resumes the
  session with it; Codex takes it in the turn it is running. The difference is Claude's
  SDK, not a choice: see "Possible later".
- **Cancel and the wall-clock cap stop the agent, then its process.** For Codex that kills
  the commands it was running — checked with one that ignores SIGTERM, SIGHUP and SIGINT.
  A command that *detached itself* (`setsid`, a daemon) survives, as it would have under
  `codex exec`: Keryx stops the app-server, not a process group it never had.

## Possible later

- **A dollar figure and a dollar cap for Codex.** It reports tokens per turn, so an API-key
  user's cost could be priced from a table and capped. A ChatGPT-plan run has no price.
- **A turn cap for Codex.** There is no flag for one; the wall-clock cap stands in.
- **The approval bridge for Codex sessions on your screen.** Codex has a hooks file, so this
  is plausible; nothing is built.
- **`CODEX_ACCESS_TOKEN`, verified.** The plumbing exists (above); it needs a run with a real
  agent identity token before it moves up.
- **Codex's own budget.** The app-server can end a turn with `sessionBudgetExceeded`; that is
  already reported as an ordinary failure, but nothing sets such a budget yet.
- **A follow-up into a running Claude turn.** A `query()` sent while Claude works folds into
  the turn at a tool boundary, but one sent after its final text starts a second turn that
  the reader never sees, so it would be lost. The way in to try: start the CLI with
  `extra_args={"replay-user-messages": None}` and see whether it echoes a queued message
  when it takes it; if so, the adapter can read on past a result while a steer is
  unacknowledged.

## Not easily possible

- **claude.ai connectors for Codex.** They belong to the Anthropic account. Codex uses
  `workspace-mcp` instead.
- **Handing a task from one agent to the other.** Session ids are not portable, so a
  follow-up always stays with the agent that started the task.
- **A spend figure for subscription use.** Neither subscription exposes what one call
  cost, so `check_billing` covers API keys only, for both agents.
- **A dollar cap on subscription runs**, for the same reason.

## Adding a third agent

Every agent runs through one session (`keryx/agents/session.py`), which already does the
progress lines, the `SPOKEN_SUMMARY:` and `RESTART_REQUIRED:` reading, the usage, the
redaction and the error handling. A new agent is:

- **one module** under `src/keryx/agents/` holding an *adapter* — a class whose
  `turn(prompt)` yields `Text`, `ToolCall`, `FileEdit`, `SessionId`, `Notice` and a final
  `Done`, plus `steer`, `interrupt` and `close` — and an `AdapterRunner` subclass whose
  `context(task)` builds its `AgentContext` and whose `connect(context, resume)` starts its
  client and returns the adapter;
- **one `BackendSpec` entry** in `keryx/agents/registry.py::BACKENDS`: its runner, its
  spoken model names, where its credentials come from (`AuthSource`), its install hint,
  instructions file, skills directory and login commands. The router, the task manager, the
  voice tools, `doctor` and `keryx setup` read that table and nothing else;
- **its name** in `AgentName` in `config/settings.py`, **its settings** in `Settings`
  (`docs/configuration.md` is regenerated from it), **a column** in the table above (a test checks), and **tests** — the
  shared `ScriptedAdapter` in `tests/agents/fakes.py` covers the session, so its own tests
  are the translation from its client's messages to those events.
