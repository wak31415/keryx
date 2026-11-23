# The tools the voice model can call

Seventeen tools are built in. They're Keryx's own machinery for a call: dispatching work,
following it, and getting off the phone. Nine more are [plugins](#plugins) that you turn on
if you want them, and anything else is [a tool of your own](#your-own-tools).

<!-- tools:start -->
| Core tool | What it does |
|---|---|
| `dispatch_task` | hand the work to a coding agent and get back a task number; `agent` names Claude or Codex when both are ready ([agents](agents.md)) |
| `list_tasks` | what is queued, running and recently finished |
| `get_task_status` | how one task is getting on |
| `get_task_result` | what a finished task found: its summary and its whole report, which follow-up questions are answered from |
| `send_followup` | add something to a task already in flight |
| `cancel_task` | stop one |
| `mark_reported` | record that a result has now been *said out loud* — the only thing that stops it riding the next call's digest |
| `recall` | search past call transcripts and past task summaries |
| `list_projects` | the project names that can be dispatched into |
| `request_callback` | call back when a task lands |
| `web_search` | answer a small factual question on the spot, through the Responses API |
| `restart_service` | restart Keryx (after the call ends), or upgrade it to the newest release |
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
first time through, and `check_calendar` and `find_document` too once Calendar or Drive is
connected. `add_calendar_event` writes, so it is never ticked for you.

| Plugin | What it does | PIN | Its secret |
|---|---|---|---|
| `slack_report` | Keryx sends you a written message on Slack, as Keryx — only when you ask for one; finished tasks' reports and PIN-lockout alerts go there too. With more than one workspace connected it takes a `workspace`, and sends to your first one unless you name another. It was `send_to_slack`, and a machine that installed it under that name needs to do nothing | yes | your Slack app's tokens (`keryx auth login slack`), or the MCP server's |
| `slack_read` | read your Slack as you, in a few seconds. Every argument is a filter and they combine: a `conversation` (a channel's name, a person's name, or a ref), a `thread`, `text` to look for, `from` a person, `only` what is `waiting` on you (a best effort) or what `mentions` you, how many `days` back, and a `question` answered in one model call over what it found. With none it says what is waiting. A name that could mean several things is asked about, never guessed | yes | a Slack workspace at the `read` level (`keryx auth login slack --access read`) |
| `slack_message` | send one message **as you**, to a person, a channel, or as a reply in a thread. It never sends on the first ask: you hear who it goes to and the exact words, and it is sent only after you say yes. No edit, no delete, no file, and never `@channel` or `@here` | yes | a Slack workspace at the `write` level (`keryx auth login slack --access write`) |
| `check_email` | answer a question about your email in about five seconds: a whole day (each thread once, answered ones left out), or a Gmail search, each alternative searched on its own and the most promising threads read in full | yes | your Google sign-in (`keryx auth login google`) and the `claude` extra |
| `check_calendar` | what is on your Google Calendar in a second or two: a day, a week, your next meeting, or a search ("when is the dentist"), across every calendar you have ticked there | yes | your Google sign-in |
| `add_calendar_event` | put one event on your own calendar and say back what it created; it invites nobody | yes | your Google sign-in |
| `find_document` | find a file in your Google Drive by its name or its text, and answer a question about what is in it from one export and one model call | yes | your Google sign-in and the `claude` extra |
| `check_billing` | what the month has cost and where it is heading, from the provider's billing API | no | `OPENAI_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY` |
| `cluster_stats` | what is free on your Slurm clusters and whether your jobs are still running | no | none: it rides the ssh login you already have open |

A plugin is two files in `~/.local/share/keryx/tools/`:

- **`<name>.py`**, one line that calls into Keryx (`keryx.plugins.<module>`), so an update
  to Keryx reaches your copy; and
- **`<name>.toml`**, its settings, with a comment on every one — edit it by hand, or walk
  `keryx setup` → Plugins again. A secret is never in it: it stays in `secrets.toml`
  (`keryx config set KEY --stdin`), or in the Google sign-in's own file.

To turn one on: `keryx setup` → Plugins, or `keryx plugins install NAME [--set KEY=VALUE]`
(for Slack the first time, `keryx auth login slack`, which gets it a token and somewhere to
send and turns it on; see below).
`keryx plugins` lists them, and why one that is on is refused (not signed in, a setting
that does not validate); `keryx plugins remove NAME` turns one off and keeps its settings
for next time. They are custom tools like your own (below), and `keryx tools` lists them
too.

`check_billing` and `cluster_stats` answer before the PIN: they cannot change anything and
read nothing of yours. `slack_report` writes to you, `slack_message` writes as you,
`slack_read`, `check_email`, `check_calendar` and `find_document` read what is yours, and
`add_calendar_event` changes it, so all seven wait for it.

**The calendar is two plugins, so reading can be on without writing.** Both run on the one
Google sign-in (`keryx auth login google`, with Calendar ticked) and answer inside the call,
with no coding agent and no model in between:

```bash
keryx plugins install check_calendar
keryx plugins install add_calendar_event --set default_minutes=30
```

`check_calendar` reads every calendar ticked in Google Calendar's own list, in your
calendar's time zone, and says the time on the clock `CLOCK_FORMAT` names; set
`calendars = "primary"` in `check_calendar.toml` for your own calendar alone.
`max_events` is how many it says at once, 100 at most. "What's my next meeting" leaves out
what has already started, what lasts all day and what you declined.
`add_calendar_event` writes to your own calendar only, one event at a time, timed or all
day. It has no way to invite anyone, repeat an event, change one or delete one: those are a
task for a coding agent, which has the whole Google toolset. It will not add an event whose
start has passed, with a quarter of an hour's leeway so that "from now" works: "Thursday
at nine", said on Thursday afternoon, is next Thursday, and
the assistant says the weekday and the date back so that a wrong week is heard. A time
with no morning or afternoon to it ("at three") is asked about, not guessed. If Google
never confirms an event, it says so and does not try again, so a bad line cannot leave you
with two.

**`find_document` finds, and reads when you ask it something.** "Find the doc about the
offsite" is one Drive search by name and by text, and you hear the best match and when it
last changed. "What does my planning doc say about hiring" also exports the best match that
can be read (a Doc or a Slides deck as text, a Sheet as its first sheet, a text file as it
is, the first 40,000 characters) and has one model call answer from it, in a few seconds.
A PDF, a Word file or an image is found but cannot be read on a call, and the assistant
says so and offers to hand it to a coding agent. It needs Drive in your Google sign-in and
the `claude` extra, like `check_email`, and its `model` and `effort` are in
`find_document.toml`. It only reads: creating, editing and sharing files are a task.

**`check_billing` needs an admin key.** The key Keryx talks to the model with can't read
billing: OpenAI wants an Admin key from the organization's admin-keys page, and Anthropic
wants an `sk-ant-admin…` key. To turn it on by hand:

```bash
keryx config set OPENAI_ADMIN_KEY --stdin     # or ANTHROPIC_ADMIN_KEY
keryx plugins install check_billing --set monthly_budget=40
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

**Connecting Slack is a choice, a link and a paste.** The choice comes first, because it
decides the app: what Keryx may see in your Slack, and where you can set it to work from.

| Level | Where you can ask Keryx for something | What Keryx can see | What you paste |
|---|---|---|---|
| `announce` (the default) | nowhere on Slack: it only posts to you | nothing | the bot token (`xoxb-`) |
| `converse` | your direct messages with the Keryx app | the messages you type to the app itself, and nothing else | and the app-level token (`xapp-`) |
| `read` | also any conversation you have added the app to, and it answers there | also every message that mentions it there (anyone's but yours is dropped); and Slack as you (every conversation you are in, their lists, the member list, search), only when you ask about messages | and your user token (`xoxp-`) |
| `write` | the same places | the same; and it can **send messages as you**, to other people and channels, when you ask for that message | the same three tokens, after the app is reinstalled |

`keryx setup` → Plugins asks the level, then shows a link that opens Slack's own page for
a new app, with Keryx's already filled in for that level: pick your workspace, create it,
install it, and copy the tokens the level needs. Paste each once, at a hidden prompt.
Keryx checks each with Slack, tells you which workspace it connected to, and finds you by
the email address you use there, so there is no channel id to look up. Nothing is saved
until every token has been checked. Then it offers to send a test message, which arrives
under Apps in Slack. The app is yours alone. From the command line it is two steps:
`keryx auth login slack --access LEVEL` says what the level means and prints the link, and
`keryx auth login slack --stdin --access LEVEL --email you@example.com` takes the tokens on
standard input, one to a line in any order (never as arguments), and does the rest.

**Keryx acts only when you mention it.** At `converse` and above, a message is acted on
only when it is yours and mentions the app: `@Keryx look into the flaky test`. A new
message starts a task; a reply under a task's report that mentions it continues that
task. Without the mention nothing happens, and nobody else's mention does anything.
Keryx puts an 👀 on a message it has acted on and says nothing else until the work is
done. None of this needs the phone PIN, and [SECURITY.md](../SECURITY.md) says what that
trade is; each reply that sets a task going again counts against the daily task cap.
Keryx starts listening when it starts, so run `keryx restart` after choosing the level.

**At `read` you can ask from anywhere, and about anything you can see.** Add the app to
a channel and mention it there: `@Keryx summarise this thread`. It reacts to your message
and **answers in that thread**, where the others in the conversation can read the answer
too, so ask in your direct messages with the app for anything you would not say in front
of them. On a call, the `slack_read` plugin answers "anything waiting for me on Slack?",
"what did Sam say about the budget?" or "read me that thread" in a few seconds (`keryx
plugins install slack_read`; the wizard offers it when you choose `read`). Slack is read
only when you ask: there is no background digest. What is read is other people's words,
and Keryx treats it as material to answer from, never as instructions.

**At `write` Keryx can send a message as you.** "Tell Sam I'm running late", "reply in
that thread that it's fixed": the `slack_message` plugin (`keryx plugins install
slack_message`) works out who that is, says the recipient and the words back to you, and
sends only after you say yes. If the name could be two people it asks which. It posts a
new message or a reply and nothing else. A coding agent working on a request of yours can
send one as well, when the request asks for exactly that message; nothing asks you first
then, which is the trade [SECURITY.md](../SECURITY.md) spells out. Lowering the level to
`read` takes all of it away at once.

**Whose messages are acted on is set once, and that is all.** The first time you connect
at `converse` or above, Keryx says which member it takes to be you. Nothing changes that
afterwards: not sending reports somewhere else (`--email`), and there is no option for it.
To connect as somebody else, `keryx auth logout slack` and log in again.

**Changing the level later.** A higher one changes the app you have, never makes a second:
`keryx auth login slack --access converse` on a workspace that is connected prints the
manifest to paste into the app's App Manifest page, and you reinstall it and hand over
the new token. A lower one needs nothing from Slack: `keryx auth login slack --access
announce` forgets the tokens it no longer uses and a running Keryx stops acting on your
messages at once. `keryx auth logout slack` forgets the workspace altogether; remove the
app in Slack to end its tokens.

**More than one workspace.** `keryx auth login slack --workspace work` connects another,
with an app of its own from the same link, its own level and its own tokens
(`keryx auth logout slack --workspace work` removes it). Reports and alerts keep going to
your first workspace; "send that to my work Slack" goes to the one you name, and a task
you asked for in one workspace is answered there and nowhere else. `keryx auth status`
lists each with its level.

The question that takes your address takes a channel or member id as well, and that is
what to give it if Slack cannot find you: your member id is in Slack under your profile,
More, Copy member ID. An app you made by hand before keeps working exactly as it was. It
can only look you up by address if it has the `users:read.email` permission.

A new token brings its destination with it. The id of a direct message belongs to the app
that opened it, so when you paste a token that is not the one Keryx has, an old `D…` id is
not kept beside it: you are looked up again (the command line needs `--email` for that, or
your Google address on file, and saves nothing without one). A member id or a channel's id
is kept. The same goes for an MCP server named in `slack_report.toml`: a token of the
plugin's own never posts to the server's channel, and once it has its own destination the
voice tool uses that pair while subagents go on using the server. If Keryx was running with
an older token, `keryx restart` moves it to the new one.

Slack is opt-in in both directions. With `slack_report` on, every finished task's whole
report is posted to your DM when it lands, so a call can stay short. Nothing else goes to
it unless you asked. When you do ask, the voice sends text with `slack_report`, and a
subagent sends it with `keryx slack report`, or (files, plots and reports included)
through the MCP server named by `mcp_server` in `slack_report.toml` where you have given
it one. Unasked, a file stays in the written report — the assistant tells you it is
there and offers to send it, rather than reading a path down the phone.

## One routing decision

Small talk, task status and small factual questions the voice answers itself — `web_search`
goes through the Responses API, because a Realtime session has no hosted search tool of its
own. So is a quick read of your mail, your calendar or a document, and putting one new
event on your calendar, when the plugin for it is on; without it, and for anything it does
not do (replying, changing an event, editing a file), that is a task too. Everything else becomes a task, and a task is just "a coding agent, on this machine" —
Claude Code or Codex ([`agents.md`](agents.md)): one kind, every tool, the repositories,
the Google services you connected (Gmail, Calendar, Drive, Docs, Sheets and Slides, from
one `keryx auth login google`), the installed skills, and subagents of its own. Nothing
classifies the work in advance.

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

**Writing one yourself** needs no clone and no build: the file runs inside Keryx, so save it
in `~/.local/share/keryx/tools/` and call. For completion and type checking in your editor,
install Keryx into the editor's environment too (`pip install keryx-voice`, or
`uv add --dev keryx-voice` in a project of your own). Inside the service a tool can import
only the standard library, `httpx`, `numpy`, `pydantic` and `keryx`, so it cannot bring a
dependency of its own.

Skills are often a better answer than a tool. Anything under `SKILLS_DIR` is listed in the
voice prompt, so a subagent already knows what it's good at without you naming it. A new
skill needs no code here at all, and no restart.
