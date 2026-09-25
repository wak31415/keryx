# Coding agents: Claude Code and Codex

Jarvis does the talking; a coding agent on your machine does the work. Two are supported,
and a task runs on whichever one it was handed to for its whole life:

- **Claude Code** (`claude`), driven through the Claude Agent SDK. The default.
- **Codex** (`codex`), OpenAI's CLI, driven as `codex exec --json`.

`AGENT_BACKEND` picks the one that does the work when you do not say, and `AGENTS_ENABLED`
lists every one a task may be sent to. With more than one enabled and signed in, you can
name it out loud — "have Codex look at the build" — and a model name picks its agent too
("use opus" is Claude, "use terra" is Codex). A follow-up always goes back to the agent that
started the task: a session belongs to the agent that issued it.

`uv run jarvis setup-agent` is the way in. It shows what is installed and signed in, runs
the login each agent is missing, runs one real task through each as a smoke test, and prints
the lines for `.env`. `uv run jarvis doctor` checks the same things on every run.

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

`CODEX_ACCESS_TOKEN` is not a ChatGPT token. Codex 0.156 does not read it from the
environment, and `codex login --with-access-token` expects an OpenAI *agent identity* token.
Jarvis logs in with it once, into a Codex home of its own (`~/.jarvis/codex`, with your
`config.toml`, `AGENTS.md` and `skills` linked in), so your own `codex login` is never
touched. That path is wired and unit-tested but has not been run against a real agent
identity token; the stored login and `CODEX_API_KEY` both have.

## What each agent can do here

Every ✅ was checked against the real CLI while the feature was built — the Slack and Gmail
rows as far as the mechanism: an MCP server handed to Codex with its secrets passed by name,
proved with a stand-in server rather than Slack or Google themselves. Anything that was not
checked is marked, and listed under "possible".

<!-- agents:start -->
| Capability | claude | codex |
|---|:---:|:---:|
| Dispatch by voice, and name the agent out loud | ✅ | ✅ |
| Follow-ups resume the same session | ✅ | ✅ `codex exec resume` |
| Progress lines in `~/.jarvis/tasks/<id>.log` | ✅ | ✅ |
| `SPOKEN_SUMMARY:` / `RESTART_REQUIRED:` | ✅ | ✅ |
| Project working directory and briefs | ✅ | ✅ |
| The per-call memory update | ✅ | ✅ when it is the default |
| API key and stored subscription login | ✅ | ✅ |
| Headless subscription token | ✅ | 🟡 wired, not verified |
| Choosing the model by name | ✅ opus, sonnet, fable, haiku | ✅ sol, terra, luna |
| Wall-clock cap (`SUBAGENT_TIMEOUT_S`) | ✅ | ✅ |
| Turn cap and dollar cap | ✅ `SUBAGENT_MAX_TURNS`, `SUBAGENT_MAX_BUDGET_USD` | — |
| Cost of each task, in dollars | ✅ | — tokens only |
| Slack, through the server `SLACK_MCP_SERVER` names | ✅ | ✅ handed over from `~/.claude.json` |
| Gmail and Calendar | ✅ claude.ai connectors | ✅ with `GOOGLE_WORKSPACE_MCP=true` |
| Its own instructions file | `~/.claude/CLAUDE.md` | `~/.codex/AGENTS.md` |
| Its own skills, listed to the voice model | `SKILLS_DIR` | `~/.codex/skills` |
| `doctor`, `setup-agent`, `--fake-agents` | ✅ | ✅ |
| Approval bridge for your on-screen sessions | ✅ | — |
<!-- agents:end -->

Two of those are worth knowing before you switch:

- **Gmail and Calendar.** Claude gets them from its claude.ai connectors, which live on the
  Anthropic account. Codex has none, so with Codex enabled, turn on `GOOGLE_WORKSPACE_MCP`
  and run `uv run jarvis setup-google` once; `doctor` warns until you have.
- **The approval bridge** (`jarvis approvals`) is for Claude Code sessions on your own
  screen that stop and ask you something. It has nothing to do with which agent Jarvis
  dispatches to, and it stays Claude Code only.

Both agents run with approvals and the sandbox off (`bypassPermissions` for Claude,
`--dangerously-bypass-approvals-and-sandbox` for Codex). The phone PIN gates the dispatch,
the same way whichever agent runs it: see [SECURITY.md](../SECURITY.md).

## Possible later

- **A dollar figure and a dollar cap for Codex.** It reports tokens per turn, so an API-key
  user's cost could be priced from a table and capped. A ChatGPT-plan run has no price.
- **A turn cap for Codex.** There is no flag for one; the wall-clock cap stands in.
- **The approval bridge for Codex sessions on your screen.** Codex has a hooks file, so this
  is plausible; nothing is built.
- **Tokens and cost on the task row.** Neither agent records them there today; they are in
  the service log.
- **`CODEX_ACCESS_TOKEN`, verified.** The plumbing exists (above); it needs a run with a real
  agent identity token before it moves up.

## Not easily possible

- **claude.ai connectors for Codex.** They belong to the Anthropic account. Codex uses
  `workspace-mcp` instead.
- **Handing a task from one agent to the other.** Session ids are not portable, so a
  follow-up always stays with the agent that started the task.
- **A spend figure for subscription use.** Neither subscription exposes what one call
  cost, so `check_billing` covers API keys only, for both agents.
- **A dollar cap on subscription runs**, for the same reason.

## Adding a third agent

One module under `src/jarvis/agents/` with an `AgentRunner`, and one entry in
`jarvis/agents/registry.py::BACKENDS` — its runner, its spoken model names, where its
credentials come from (`AuthSource`), its install hint, instructions file, skills
directory and login commands. The router, the task manager, the voice tools, `doctor` and
`setup-agent` read that table and nothing else. Add its name to `AgentName` in
`config.py`, a column to the table above (a test checks), and its settings to
`.env.example`.
