# The tools the voice model can call

There are twenty, and they fall into two groups that you should treat very differently.

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

**The rest are examples.** They are the tools one person actually wanted, kept here
because they are worked examples of the shape rather than because you need them.
`cluster_stats` reads Slurm clusters through an ssh guard you write yourself, and is not
offered at all until `CLUSTERS` and `CLUSTER_SSH_GUARD` are set;
`check_billing` reads an API bill; `check_email` needs `jarvis auth login gmail` (read-only) and the
`claude` extra, and waits behind the PIN; the approval pair is for someone who uses Claude Code
on the same machine. Read them for the pattern, then delete them and write your own.

| Example tool | What it does | Why it is a tool and not a task |
|---|---|---|
| `send_to_slack` | send a written message to the Slack DM — only when asked | the answer belongs somewhere you can read later |
| `check_billing` | what the month has cost, read off the provider's billing API | two numbers, wanted mid-sentence |
| `cluster_stats` | what is free and what is running on the Slurm clusters | same — "is my job still going" is a question, not a job |
| `check_email` | answer a question about your email: a whole day as a few spoken lines (each thread once, answered threads left out), or a Gmail search with the newest few matches read in full | a task took minutes to read the inbox; this is one Gmail pass and one model call, about five seconds |
| `list_pending_approvals` | what a Claude Code session on the desktop is waiting on | you are being asked, not asking |
| `answer_approval` | read that prompt out and offer the keypad — it cannot approve anything itself | the keypad decides, never the transcription |
<!-- tools:end -->

`check_billing` and `cluster_stats` are deliberately not PIN-gated: they cannot change
anything and read nothing of yours. With `web_search`, `submit_pin` and `end_session` they
are the only tools an inbound caller reaches before the PIN; everything else waits for it.
On a call *Jarvis placed to your own number*, five more open up without it —
`send_followup`, `request_callback`, `mark_reported` and the two approval tools — because
reaching that phone proves something an inbound number cannot. See the [security model](https://github.com/wak31415/jarvis-voice-agent/wiki/Security-Model).

Slack is opt-in: nothing goes to it unless you asked for it. When you do ask, the voice
sends text with `send_to_slack` and subagents send files, plots and reports through the
same Slack app, as the Slack MCP server you name in `SLACK_MCP_SERVER`. Unasked, a file
stays in the written report — Jarvis tells you it is there and offers to send it, rather
than reading a path down the phone.

## One routing decision

Small talk, task status and small factual questions the voice answers itself — `web_search`
goes through the Responses API, because a Realtime session has no hosted search tool of its
own. Everything else becomes a task, and a task is just "a coding agent, on this machine" —
Claude Code or Codex ([`agents.md`](agents.md)): one kind, every tool, the repositories,
Gmail and Calendar, the installed skills, and subagents of its own. Nothing classifies the
work in advance.

That is why the example tools are short. **The default answer to "can Jarvis do X" is "ask
Claude to do X"** — a task already has your machine, your repositories, your mailbox and
every skill you have installed. A tool only earns its place when the answer is needed
*inside the call*, in the second or two before a silence gets awkward. Half a minute of
nothing while a subagent goes and looks is the thing a tool exists to avoid, and it is the
only thing it buys you.

## Writing your own

The quickest way is to ask for one out loud:

> *"In the jarvis project, add a tool called `next_train` that reads the departure board
> for my station and tells me the next two trains. Same shape as `check_billing`."*

That is an ordinary task. The subagent has the repository, the tests and this file, and
`prompts/subagent_suffix.md` already tells it how work here is expected to end. It will
need the PIN, like every dispatch from the phone, and a `.py` change needs a restart before
the tool exists — say *"restart yourself"* when it is done and Jarvis will ring you back
once it is up.

What it will do, and what to check if you are writing it by hand:

1. **A new `src/jarvis/tools/builtin_<domain>.py`**, exporting one `register_*` function.
   The existing five are `builtin_comms`, `builtin_billing`, `builtin_tasks`,
   `builtin_restart` and `builtin_session`; `builtin_common` holds the wording, the
   argument parsing and the two gates. If the tool talks to something outside this
   machine, the *client* is a separate module under `src/jarvis/integrations/` —
   `billing`, `cluster`, `slack` and `web_search` are the four that exist — and the
   `builtin_*` module only registers it.
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
   example of the protocol-plus-fake shape and `tests/tools/test_builtin.py` for how it
   is driven.
6. **A description written to be *heard*.** The model reads it to decide when to reach for
   the tool, so say when to use it and when not to. Look at how `check_billing`'s
   description names the actual phrasings — "what am I spending", "what has Claude cost" —
   rather than describing the API it calls.

Two of the example tools are written up end to end as templates to work from:
[`check_billing`](https://github.com/wak31415/jarvis-voice-agent/wiki/Worked-Example-check_billing) for the read-only-API shape, and
[`cluster_stats`](https://github.com/wak31415/jarvis-voice-agent/wiki/Worked-Example-cluster_stats) for reaching outside the machine
safely.

Skills are the other half of this and often the better answer. Anything under `SKILLS_DIR`
is listed in the voice prompt, so a subagent already knows what it is good at without you
naming it — a new skill needs no code here at all, and no restart.
