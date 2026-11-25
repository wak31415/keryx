---
name: keryx-setup
description: >-
  Set up a new Keryx install from the command line: read what is configured, set what you
  know through `keryx config` (secrets never on the command line), check the sign-ins with
  `keryx auth status`, interview the owner briefly, draft project summaries for the projects
  they pick, seed the memory with `keryx memory seed`, and hand over to `keryx setup` for
  what only they can do. Use when someone has just cloned or installed the Keryx voice
  agent, says "set up Keryx", "onboard me", or asks what Keryx knows about them.
---

# Setting up a new Keryx

Keryx answers the phone knowing only what it is handed at the top of each call: a memory
document, a short brief per project, and the names of the projects a task can be pointed
at. Before that it needs a voice key, a coding agent that can run, and — for the phone — a
Twilio number and a PIN. You are the session that gets it there, from the command line.

Start by reading the machine's own instructions, which carry this machine's paths:

```bash
keryx setup --agent-instructions
```

In a clone of the repository rather than an install, run every `keryx` command here as
`uv run keryx`.

Everything below is those instructions plus the rules for talking to the owner.

## The rules, and they are not preferences

- **Nothing is scanned, written or sent without them seeing it first.** Show every answer
  back, every summary, every proposed edit. "I'll just have a quick look through your
  repositories" is exactly the thing this skill exists not to do.
- **The memory and the briefs are sent to the realtime provider on every call**, as part of
  the system prompt, re-sent each time. Say that to them in those words, before they answer
  anything, and leave out of both anything they would not send to OpenAI.
- **A secret never goes on the command line**: `keryx config set KEY --stdin` or
  `--from-env VAR`, never `keryx config set KEY value`. If you do not already have a secret
  in your environment, do not ask them to paste it to you — `keryx setup` asks for it
  hidden, and that is theirs to run.
- **Never read Keryx's own directories** — `~/.config/keryx/`, `~/.local/share/keryx/`,
  or `~/.local/state/keryx/`: `secrets.toml` and `pin` are every key
  and the PIN, and the call transcripts carry the spoken PIN. The one place you write there
  is `~/.local/share/keryx/projects/<name>.md` (`keryx config path` says where, if
  `DATA_DIR` moved it).
- **Never write `memory.md` yourself.** Keryx owns that file and rewrites it after every
  call. Everything you agree on goes in through `keryx memory seed`.
- **Never choose a PIN for them.** A PIN you picked is a PIN in a transcript. `keryx setup`
  asks them for one at the keyboard; the other way is the first phone call, which they make.
- **Never scan a project they did not pick**, and never pass `--force` to
  `keryx memory seed` without asking: it replaces a memory that calls have already written.

## 1. See what is there

```bash
keryx config list --json
keryx doctor --json
keryx auth status --json
```

`config list` is every setting with whether it is set and where from — a secret's value is
never in it. `doctor` groups each check by `section` with a `state` of ok, missing or failed.
`auth status` is every sign-in; Google and Slack are marked `optional` in it, because Keryx
runs without them.

Then tell them, in three or four sentences: what Keryx does with what you are about to
collect, that it goes to the realtime provider on every call, and that nothing is written
until they have seen it.

## 2. Set what you know

Plain settings go straight in: `keryx config set KEY VALUE [KEY VALUE …]`. Only the
ones you were told or can see — a `PUBLIC_HOST` they named, `ALLOWED_CALLERS` they gave you
in E.164. A coding agent with no sign-in: `keryx auth login claude` (or `codex`)
runs its own login, which opens a browser or prints a code — theirs to finish, not yours.

## 3. The plugins they want

Nine voice tools are plugins, off until they are turned on: `slack_report` and
`check_email` (most people want both), `slack_read` (it reads their Slack as them, and
is turned on by the Slack login when they switch reading on there), `slack_message` (it
sends a message *as them*, and is turned on by the Slack login when they switch sending
on there, which is theirs to say: never install it yourself because they said yes to
something else), `check_calendar`,
`add_calendar_event` (it writes to their calendar, so it is its own yes), `find_document`,
`check_billing` and `cluster_stats`. Ask which they want; do not turn one on unasked.

```bash
keryx plugins --json            # each one: on or off, its files, why one is refused
keryx plugins install check_billing --set monthly_budget=40
keryx plugins hosts --json      # the ssh hosts cluster_stats could ask
keryx plugins install cluster_stats --cluster HOST=PARTITION
```

A plugin's secret is never a `--set`: the billing admin keys are
`keryx config set KEY --stdin` (theirs to type, through `keryx setup` if you do not have
it), and email, the two calendar plugins and `find_document` need the Google sign-in,
`keryx auth login google` (the sign-ins step of the instructions; the same sign-in gives
the agents Calendar, Drive, Docs, Sheets and Slides, and the address they paste back goes
in on standard input, never into the command). That sign-in first needs a Google OAuth
client of their own, because Google's terms do not let an open-source app ship one. The
best way is theirs to run at a terminal: `keryx auth login google` with no client on file
opens Google's Personal mode page, shows the clicks and asks them for the downloaded file.
Without a terminal, relay the page (the refusal names it), and once they give you the path
of the file Google downloaded, pass it with `--client-file`. A plugin installed before its
service is connected is refused with the
reason, and loads by itself once the sign-in includes it.

Slack is a sign-in too, and it turns `slack_report` on by itself (it was called
`send_to_slack`; a machine that installed it under that name needs nothing done):

```bash
keryx auth login slack                        # says what there is to switch on: read it to them
keryx auth login slack --allow none           # what they chose: a link and steps, to relay
keryx auth login slack --stdin --allow none   # theirs to run: it asks for each token hidden
```

Run by you, with no terminal, each of these prints and ends. At a terminal of their own,
`keryx auth login slack` with no option is the whole sign-in in one command: it shows a
box to tick for each thing Keryx may do, prints the steps for exactly that and then
waits for the tokens, so someone who would rather do it all themselves runs only that.
Offer it first.

**What Keryx may do in their Slack is theirs to choose, and it comes first**, because it
decides the app the link creates. Sending them reports and files is always on. The first
command prints the three things that may be added, each on its own: `requests` (it acts
on what they type to the app, which is named after their assistant: every message of
theirs there, with no mention needed, can resume or start work *without the phone
PIN*), `read` (it reads Slack as them when they ask about messages; with `requests` as
well, they can also mention it in a conversation it was added to, where it answers in
that thread for the others there to read too) and `send` (it sends messages as them, to
other people and channels, after reading each back and a yes). Read them those
sentences and ask. Never choose `requests`, `read` or `send` for them, and never suggest
one to save a step. What they say goes on both of the other commands as `--allow`, once
for each (`--allow requests --allow read`); `--allow none` is reports and files alone,
which is what to use when they give no answer **on a workspace that is not connected
yet**. Nothing is guessed: with no `--allow` on a workspace that is not connected, the
command prints the choices and saves nothing. **On a workspace that is connected, never
pass `--allow none` by default**: `--allow` there says everything that stays on, and
whatever it leaves out is switched off at once, so `none` switches off everything they
had on. Read what is on from `keryx auth status --json` (`slack.workspaces[].allow`)
and pass exactly that selection, unless they asked you for a change.
A first connection takes two tokens whatever is ticked, their own user token and
the bot token (the user token is how Slack says which member they are; there is no
address to give and nothing is asked about where to send: reports go to them). Keryx
keeps the user token only while `read` or `send` is on: with neither it is asked once
who they are and not saved, and a workspace that is already connected, whose owner is
on file, is asked for none. With
`requests`, the app-level token is asked for
first (on standard input it goes with the other two, one to a line, in any order). The command names the member it takes them to be: read them
that line. It is settled then and nothing changes it afterwards; connecting as somebody
else is `keryx auth logout slack` and a new login, which is theirs to ask for. Running
the login again is how they change what is on: at a terminal the boxes come up ticked
as they stand, and without one `--allow` says everything that is to stay on. Whatever
it leaves out is switched off at once, with nothing asked of Slack and before anything
else the command does, with or without `--stdin`; whatever it
adds prints the manifest to paste into the app they have. A machine
that connected Slack before user tokens were asked for keeps working as it is: only if
they want more switched on, or an owner on file, do they run the login again, and the
first command then prints how to update the app they have. The same goes for an app
made before Keryx could send files: the first command asks Slack what the app may do,
and when it lacks `files:write` it prints the manifest to paste into the app and the
reinstall that follows. Relay those steps; text reports work meanwhile.

The link opens Slack's own page for a new app with theirs already filled in. The steps
the first command prints follow Slack's console, one visit to each page, and the tokens
are asked for in the order they are copied. Say the places as the steps do, as paths:

- with `requests`, before installing: `Basic Information > App-Level Tokens >
  Generate Token and Scopes`, with the `connections:write` scope, gives the app-level
  token (`xapp-…`);
- they install the app to their workspace and click Allow;
- `OAuth & Permissions > OAuth Tokens` then lists the User OAuth Token (`xoxp-…`) and,
  below it, the Bot User OAuth Token (`xoxb-…`): they copy both, in that order.

Do not ask them to paste a token to you: they run the second command themselves. It checks each token with Slack, takes the member their
user token belongs to as them (so there is no address to give and no channel id to look
up), and sends one test message — ask them whether it arrived. An app they connected
before keeps working. A second
workspace is the same two commands with `--workspace NAME`, and `keryx auth logout slack
[--workspace NAME]` disconnects one.

Offer `cluster_stats` only a host whose `control_master` is true — Keryx never opens a
connection of its own — and ask them which partition its GPUs are in rather than guessing.
Every other setting of a plugin is in the TOML file `keryx plugins` names.

Keryx can also file bug reports and feature requests about itself — something they tell
it on a call — as issues on its GitHub repository. That one is not yours to turn on, even
if they ask you to: an issue is public, so it is off until they turn it on themselves in
`keryx setup` (the Issue reports section, which also signs `gh` in). Never set
`ISSUE_REPORTING` or `ISSUE_REPO`; say in the hand-over that the section is waiting.

## 4. Interview, briefly

Five questions, and stop:

1. What should Keryx call them? (`keryx config set OWNER_NAME …`)
2. What do they work on — the one-paragraph version?
3. Which machines do they use, and where do their repositories live?
4. How do they like to be answered: the short answer, or the reasoning with it?
5. What is worth a phone call when a job finishes, and what can wait?

Keep their own words. Do not expand an answer into three facts, and do not invent a sixth
question because the fifth was interesting.

## 5. The projects they pick

Show them the project names from `projects` in the memory report (`keryx memory seed
--file - --json < /dev/null` writes nothing and prints it) — the list, not the contents —
and ask which ones matter enough for Keryx to know about. Draft a summary only for those,
and only after they have seen the list.

For each one, read its `README.md` and draft `~/.local/share/keryx/projects/<name>.md`:

- Written to be **heard**, not read: what the project is, what state it is in, and what the
  words in it mean out loud. No build commands, no directory trees, no code fences.
- At most **1,500 characters**. All of them together are capped at 6,000, and one past that
  is dropped whole from the prompt rather than truncated.
- Nothing from `.env` files, keys or credential stores.
- A repository's own `.keryx-brief.md` wins over yours; leave a project that has one alone.

Show each draft in full and write it only once they say yes.

## 6. Propose the subagents' own map

The subagents are ordinary sessions of whichever agent runs them, so they read that agent's
own instructions file: `~/.claude/CLAUDE.md` for Claude, `~/.codex/AGENTS.md` for Codex.
That file, not Keryx's memory, is where "my repositories live under ~/code" belongs. Draft
the lines that are missing, for each agent in `subagent_memories` — the same lines in each,
so the agents agree. Show every edit before you make it, and append rather than rewrite.

## 7. Seed the memory

Turn their answers into short standing facts, one per line, and show them the list. Then:

```bash
printf '%s\n' \
  'Prefers the short answer first.' \
  'Works on the orchard sensor rig and a weather dashboard.' \
  'Ring for anything that fails; everything else can wait for the next call.' \
  | keryx memory seed --file - --json
```

Exit 1 means there was already a memory there, and that is theirs to decide about.

## 8. Hand over

`keryx doctor --json` once more, then tell them in a few lines:

- **Run `keryx setup`.** It walks only what is left, and what is left is theirs:
  the secrets you did not have, browser sign-ins, **the PIN** (nothing of theirs is read out
  on the phone and nothing can be dispatched until one exists; the first call can set one
  instead, and whoever calls first sets it), and approving where the Twilio number points.
- `keryx memory` shows what it remembers; it is plain markdown they can edit.
- After every authorized call, Keryx folds that call into the memory itself. This was the
  first draft, not the last word.
