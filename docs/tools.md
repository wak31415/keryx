# The tools the voice model can call

Seventeen are built in, and they fall into two groups that you should treat very
differently. Four more are **plugins** you turn on if you want them (below), and anything
else is a tool of your own.

**The core is the machinery of a call** — dispatching work, following it, and getting off
the phone. It is the same for everybody and it is not where you should be making changes:
several of these carry rulings that are easy to break by accident. `mark_reported` in
particular is the only thing that records that a result was actually *said out loud*, and
the digest at the top of your next call depends on it.

<!-- tools:start -->
| Core tool | What it does |
|---|---|
| `dispatch_task` | hand the work to a coding agent and get back a task number; `agent` names Claude or Codex when both are ready ([agents](agents.md)) |
| `list_tasks` | what is queued, running and recently finished |
| `get_task_status` | how one task is getting on |
| `get_task_result` | the spoken summary a finished task produced |
| `send_followup` | add something to a task already in flight |
| `cancel_task` | stop one |
| `mark_reported` | record that a result has now been *said out loud* — the only thing that stops it riding the next call's digest |
| `recall` | search past call transcripts and past task summaries |
| `list_projects` | the project names that can be dispatched into |
| `request_callback` | call back when a task lands |
| `web_search` | answer a small factual question on the spot, through the Responses API |
| `restart_service` | restart Jarvis (after the call ends) |
| `set_config` | change one of Jarvis's own settings — only those the running service may (`jarvis config list`); saved, applied at the next restart |
| `submit_pin` | check a spoken PIN |
| `end_session` | hang up |

**The approval pair is for someone who uses Claude Code on the same machine.**

| Approval tool | What it does | Why it is a tool and not a task |
|---|---|---|
| `list_pending_approvals` | what a Claude Code session on the desktop is waiting on | you are being asked, not asking |
| `answer_approval` | read that prompt out and offer the keypad — it cannot approve anything itself | the keypad decides, never the transcription |
<!-- tools:end -->

`web_search`, `submit_pin` and `end_session` are the only built-in tools an inbound caller
reaches before the PIN (with the four that read back the briefing — see the security model);
everything else waits for it. On a call *Jarvis placed to your own number*, five more open
up without it — `send_followup`, `request_callback`, `mark_reported` and the two approval
tools — because reaching that phone proves something an inbound number cannot. See the
[security model](https://github.com/wak31415/jarvis-voice-agent/wiki/Security-Model).

## Plugins

The tools only some people want are plugins: none is offered until you turn it on, and
turning one on or off, or changing its settings, needs no restart — each call reads them
afresh. Slack and email are the ones most people want, so `jarvis setup` ticks them the
first time through.

| Plugin | What it does | PIN | Its secret |
|---|---|---|---|
| `send_to_slack` | send a written message to your Slack DM — only when you ask for one; PIN-lockout alerts go there too | yes | `SLACK_BOT_TOKEN`, or the MCP server's |
| `check_email` | answer a question about your email in about five seconds: a whole day (each thread once, answered ones left out), or a Gmail search with the newest few read in full | yes | the read-only Gmail sign-in (`jarvis auth login gmail`) and the `claude` extra |
| `check_billing` | what the month has cost and where it is heading, from the provider's billing API | no | `OPENAI_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY` |
| `cluster_stats` | what is free on your Slurm clusters and whether your jobs are still running | no | none: it rides the ssh login you already have open |

A plugin is two files in `~/.local/share/jarvis/tools/`:

- **`<name>.py`**, one line that calls into Jarvis (`jarvis.plugins.<module>`), so an update
  to Jarvis reaches your copy; and
- **`<name>.toml`**, its settings, with a comment on every one — edit it by hand, or walk
  `jarvis setup` → Plugins again. A secret is never in it: it stays in `secrets.toml`
  (`jarvis config set KEY --stdin`), or in the Gmail sign-in's own file.

To turn one on: `jarvis setup` → Plugins, or `jarvis plugins install NAME [--set KEY=VALUE]`.
`jarvis plugins` lists them, and why one that is on is refused (not signed in, a setting
that does not validate); `jarvis plugins remove NAME` turns one off and keeps its settings
for next time. They are custom tools like your own (below), and `jarvis tools` lists them
too.

`check_billing` and `cluster_stats` answer before the PIN: they cannot change anything and
read nothing of yours. `send_to_slack` writes as you and `check_email` reads your mail, so
both wait for it.

**`cluster_stats` needs nothing written.** It asks Slurm over an ssh ControlMaster you
already have open (`ControlMaster auto` in `~/.ssh/config`), and never opens a connection
of its own: it checks the master's local socket (`ssh -O check`) first, and says "the
login has expired" rather than dialling out, because where login is two-factor a
connection attempt nobody can answer hangs, and a storm of them gets an address banned.
`jarvis plugins hosts` lists the hosts it could ask; the wizard shows them, reads each
chosen host's partitions from Slurm, and then installs it, writes it as a template for you
to finish, or does nothing. A host without a ControlMaster is never offered. If you already
run your own guard script, name it as `guard` in `cluster_stats.toml`; it is optional.

Slack is opt-in in both directions: nothing goes to it unless you asked. When you do ask,
the voice sends text with `send_to_slack` and subagents send files, plots and reports
through the same Slack app, as the MCP server named by `mcp_server` in
`send_to_slack.toml`. Unasked, a file stays in the written report — Jarvis tells you it is
there and offers to send it, rather than reading a path down the phone.

Upgrading from before plugins, when these were settings (`CLUSTERS`, `SLACK_CHANNEL_ID`,
`BILLING_MONTHLY_BUDGET`, …): `jarvis doctor` names any still in `config.toml`, and
`jarvis plugins install --from-settings` moves them into the plugins' files and turns on
every one that was offered before.

## One routing decision

Small talk, task status and small factual questions the voice answers itself — `web_search`
goes through the Responses API, because a Realtime session has no hosted search tool of its
own. Everything else becomes a task, and a task is just "a coding agent, on this machine" —
Claude Code or Codex ([`agents.md`](agents.md)): one kind, every tool, the repositories,
Gmail and Calendar, the installed skills, and subagents of its own. Nothing classifies the
work in advance.

That is why the plugins are short. **The default answer to "can Jarvis do X" is "ask
Claude to do X"** — a task already has your machine, your repositories, your mailbox and
every skill you have installed. A tool only earns its place when the answer is needed
*inside the call*, in the second or two before a silence gets awkward. Half a minute of
nothing while a subagent goes and looks is the thing a tool exists to avoid, and it is the
only thing it buys you.

## Your own tools

The quickest way to give Jarvis a new ability is to ask for one out loud:

> *"Give yourself a way to tell me the next two trains from my station."*

That is an ordinary task (it needs the PIN, like every dispatch from the phone), and the
tool it produces is **yours, not Jarvis's**: a Python file in `~/.local/share/jarvis/tools/`
(`DATA_DIR/tools`), never in this repository and never committed. Every call reads that
directory afresh when it starts, so the tool is there from your next call — no restart.

```python
from jarvis.tools.custom import custom_tool

@custom_tool(
    description="The next two trains from the owner's station. Say both times in one "
                "sentence; do not read the platform or the operator.",
    needs_pin=False,
)
def next_train(ctx, args):
    ...
    return {"trains": ["8:14", "8:31"]}
```

Every subagent is told where these go and is pointed at
[`skills/jarvis-custom-tools/SKILL.md`](../skills/jarvis-custom-tools/SKILL.md), which is
the whole contract: the file, the wording, credentials, and how to check it. In short:

- **`needs_pin`** is the gate, and it defaults to `True`: the tool answers only once the
  PIN is given. `needs_pin=False` answers anyone who rings, and is for what is public
  anyway.
- **A tool cannot take a built-in's name**, so nothing here can stand in for `submit_pin`
  or `dispatch_task`; and a file or directory anyone but you could write is refused
  unread, because loading it runs its code inside the service.
- **A broken file costs that file.** It is logged and skipped; the call goes on with every
  other tool. A handler that raises, or runs past its `timeout_s` (20 seconds by default),
  hands the model an error it can say.
- **`jarvis tools`** lists what the directory holds and what the next call would refuse,
  and exits 1 while anything is refused.

## Adding a built-in tool

A tool that belongs in Jarvis for everyone — one you would send upstream — goes in the
repository instead. Ask for it the same way, naming the project:

> *"In the jarvis project, add a built-in tool called `next_train` … Same shape as
> `web_search`."*

The subagent has the repository, the tests and this file, and
`prompts/subagent_suffix.md` already tells it how work here is expected to end. A `.py`
change needs a restart before the tool exists — say *"restart yourself"* when it is done
and Jarvis will ring you back once it is up.

What it will do, and what to check if you are writing it by hand:

1. **A new `src/jarvis/tools/builtin_<domain>.py`**, exporting one `register_*` function.
   The existing four are `builtin_comms`, `builtin_tasks`, `builtin_restart` and
   `builtin_session`; `builtin_common` holds the wording, the argument parsing and the
   gates. If the tool talks to something outside this machine, the *client* is a separate
   module under `src/jarvis/integrations/` and the `builtin_*` module only registers it.
   A tool only some people want is a plugin instead (`src/jarvis/plugins/`: a module, a
   pair of templates and a row in `PLUGINS`).
2. **One line in `src/jarvis/tools/builtin.py`**, which is only a composition root. Where
   you put that line matters: *the order it calls the register functions in is the order
   the tools are offered to the model.*
3. **A row in the table above.** `tests/test_docs_sync.py` compares the registrations
   against this file and fails if they disagree, in either direction.
4. **`pin_gate(ctx, settings)` first, unless the tool can neither change anything nor read
   anything of yours.** Caller ID is spoofable, so before the PIN a phone caller gets
   nothing private and leaves nothing behind. A tool that only reads a public number may
   skip it — asking what a number is should not need a PIN — but it has to be added to
   `UNGATED` in `tests/tools/test_builtin.py` on purpose, or the test that walks every tool
   fails, and a tool that can run a command needs a better reason than convenience.
5. **A fake behind a `Protocol`**, never the real service. Nothing in the test suite
   touches the network or hardware; see `jarvis/integrations/billing.py` for a small
   example of the protocol-plus-fake shape and `tests/plugins/test_billing.py` for how it
   is driven.
6. **A description written to be *heard*.** The model reads it to decide when to reach for
   the tool, so say when to use it and when not to. Look at how `check_billing`'s
   description names the actual phrasings — "what am I spending", "what has Claude cost" —
   rather than describing the API it calls.

Two of the plugins are written up end to end as templates to work from:
[`check_billing`](https://github.com/wak31415/jarvis-voice-agent/wiki/Worked-Example-check_billing) for the read-only-API shape, and
[`cluster_stats`](https://github.com/wak31415/jarvis-voice-agent/wiki/Worked-Example-cluster_stats) for reaching outside the machine
safely.

Skills are the other half of this and often the better answer. Anything under `SKILLS_DIR`
is listed in the voice prompt, so a subagent already knows what it is good at without you
naming it — a new skill needs no code here at all, and no restart.
