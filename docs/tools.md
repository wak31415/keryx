# The tools the voice model can call

Seventeen tools are built in. They're Keryx's own machinery for a call: dispatching work,
following it, and getting off the phone. Four more are [plugins](#plugins) that you turn on
if you want them, and anything else is [a tool of your own](#your-own-tools).

<!-- tools:start -->
| Core tool | What it does |
|---|---|
| `dispatch_task` | hand the work to a coding agent and get back a task number; `agent` names Claude or Codex when both are ready ([agents](agents.md)) |
| `list_tasks` | what is queued, running and recently finished |
| `get_task_status` | how one task is getting on |
| `get_task_result` | the spoken summary a finished task produced |
| `send_followup` | add something to a task already in flight |
| `cancel_task` | stop one |
| `mark_reported` | record that a result has now been *said out loud*, which stops it riding the next call's digest (a result Keryx announced mid-call is also stamped once it starts playing) |
| `recall` | search past call transcripts and past task summaries |
| `list_projects` | the project names that can be dispatched into |
| `request_callback` | call back when a task lands |
| `web_search` | answer a small factual question on the spot, through whichever search `WEB_SEARCH` picks (SearXNG, Google through Gemini, OpenAI, or ddgs) |
| `restart_service` | restart Keryx (after the call ends) |
| `set_config` | change one of Keryx's own settings — only those the running service may (`keryx config list`); saved, applied at the next restart |
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
everything else waits for it. On a call *Keryx placed to your own number*, five more open
up without it — `send_followup`, `request_callback`, `mark_reported` and the two approval
tools — because reaching that phone proves something an inbound number cannot. See the
[security model](https://github.com/wak31415/keryx/wiki/Security-Model).

## Plugins

The tools only some people want are plugins: none is offered until you turn it on, and
turning one on or off, or changing its settings, needs no restart — each call reads them
afresh. Slack and email are the ones most people want, so `keryx setup` ticks them the
first time through.

| Plugin | What it does | PIN | Its secret |
|---|---|---|---|
| `send_to_slack` | send a written message to your Slack DM — only when you ask for one; PIN-lockout alerts go there too | yes | `SLACK_BOT_TOKEN`, or the MCP server's |
| `check_email` | answer a question about your email in about five seconds: a whole day (each thread once, answered ones left out), or a Gmail search with the newest few read in full | yes | the read-only Gmail sign-in (`keryx auth login gmail`) and the `claude` extra |
| `check_billing` | what the month has cost and where it is heading, from the provider's billing API | no | `OPENAI_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY` |
| `cluster_stats` | what is free on your Slurm clusters and whether your jobs are still running | no | none: it rides the ssh login you already have open |

A plugin is two files in `~/.local/share/keryx/tools/`:

- **`<name>.py`**, one line that calls into Keryx (`keryx.plugins.<module>`), so an update
  to Keryx reaches your copy; and
- **`<name>.toml`**, its settings, with a comment on every one — edit it by hand, or walk
  `keryx setup` → Plugins again. A secret is never in it: it stays in `secrets.toml`
  (`keryx config set KEY --stdin`), or in the Gmail sign-in's own file.

To turn one on: `keryx setup` → Plugins, or `keryx plugins install NAME [--set KEY=VALUE]`.
`keryx plugins` lists them, and why one that is on is refused (not signed in, a setting
that does not validate); `keryx plugins remove NAME` turns one off and keeps its settings
for next time. They are custom tools like your own (below), and `keryx tools` lists them
too.

`check_billing` and `cluster_stats` answer before the PIN: they cannot change anything and
read nothing of yours. `send_to_slack` writes as you and `check_email` reads your mail, so
both wait for it.

**`check_billing` needs an admin key.** The key Keryx talks to the model with can't read
billing: OpenAI wants an Admin key from the organization's admin-keys page, and Anthropic
wants an `sk-ant-admin…` key. To turn it on by hand:

```bash
uv run keryx config set OPENAI_ADMIN_KEY --stdin     # or ANTHROPIC_ADMIN_KEY
uv run keryx plugins install check_billing --set monthly_budget=40
```

The spend it reports is the whole organization's, or one project's or workspace's if you
set that in `check_billing.toml`. It can't be narrowed to one API key. The month-end figure
is the tool's own straight-line estimate, and the assistant says so.

**`cluster_stats` uses the ssh login you already have open.** It needs an ssh ControlMaster
to each cluster in `~/.ssh/config`, for example:

```
Host mycluster
    HostName login.example.edu
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 8h
```

Log in once at your desk, then turn it on in `keryx setup` → Plugins, or by hand with
`keryx plugins install cluster_stats --cluster mycluster=gpu`: the ssh alias, which is also
the name you say on the phone, and the partition its GPUs are in. `keryx plugins hosts`
lists the hosts it could ask. A host without a ControlMaster is never offered.

It never opens a connection of its own. It checks the master's local socket first, and if
the login has expired, it says so instead of dialing out: where login is two-factor, a
connection nobody can answer hangs, and a storm of them can get an address banned. To fix
an expired login, open the master again at your desk, for example with `ssh -fN mycluster`.
If you already run a guard script of your own, name it as `guard` in `cluster_stats.toml`.

Slack is opt-in in both directions: nothing goes to it unless you asked. When you do ask,
the voice sends text with `send_to_slack` and subagents send files, plots and reports
through the same Slack app, as the MCP server named by `mcp_server` in
`send_to_slack.toml`. Unasked, a file stays in the written report — the assistant tells you it is
there and offers to send it, rather than reading a path down the phone.

## One routing decision

Small talk, task status and small factual questions the voice answers itself — `web_search`
goes through the Responses API, because a Realtime session has no hosted search tool of its
own. Everything else becomes a task, and a task is just "a coding agent, on this machine" —
Claude Code or Codex ([`agents.md`](agents.md)): one kind, every tool, the repositories,
Gmail and Calendar, the installed skills, and subagents of its own. Nothing classifies the
work in advance.

That is why the plugins are short. **The default answer to "can Keryx do X" is "ask
Claude to do X"** — a task already has your machine, your repositories, your mailbox and
every skill you have installed. A tool only earns its place when the answer is needed
*inside the call*, in the second or two before a silence gets awkward. Half a minute of
nothing while a subagent goes and looks is the thing a tool exists to avoid, and it is the
only thing it buys you.

## Your own tools

The quickest way to give Keryx a new ability is to ask for one out loud:

> *"Give yourself a way to tell me the next two trains from my station."*

That is an ordinary task (it needs the PIN, like every dispatch from the phone), and the
tool it produces is **yours, not Keryx's**: a Python file in `~/.local/share/keryx/tools/`
(`DATA_DIR/tools`), never in this repository and never committed. Every call reads that
directory afresh when it starts, so the tool is there from your next call — no restart.

```python
from keryx.tools.custom import custom_tool

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
[`skills/keryx-custom-tools/SKILL.md`](../skills/keryx-custom-tools/SKILL.md), which is
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
- **`keryx tools`** lists what the directory holds and what the next call would refuse,
  and exits 1 while anything is refused.

Skills are often a better answer than a tool. Anything under `SKILLS_DIR` is listed in the
voice prompt, so a subagent already knows what it's good at without you naming it. A new
skill needs no code here at all, and no restart.
