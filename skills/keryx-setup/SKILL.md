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
uv run keryx setup --agent-instructions
```

Everything below is those instructions plus the rules for talking to the owner.

## The rules, and they are not preferences

- **Nothing is scanned, written or sent without them seeing it first.** Show every answer
  back, every summary, every proposed edit. "I'll just have a quick look through your
  repositories" is exactly the thing this skill exists not to do.
- **The memory and the briefs are sent to the realtime provider on every call**, as part of
  the system prompt, re-sent each time. Say that to them in those words, before they answer
  anything, and leave out of both anything they would not send to OpenAI (unless
  `VOICE_BASE_URL` is a voice server of their own, when the provider is that server).
- **A secret never goes on the command line**: `keryx config set KEY --stdin` or
  `--from-env VAR`, never `keryx config set KEY value`. If you do not already have a secret
  in your environment, do not ask them to paste it to you — `keryx setup` asks for it
  hidden, and that is theirs to run.
- **Never read Keryx's own directories** — `~/.config/keryx/`, `~/.local/share/keryx/`,
  `~/.local/state/keryx/`, or an old `~/.jarvis/`: `secrets.toml` and `pin` are every key
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
uv run keryx config list --json
uv run keryx doctor --json
uv run keryx auth status --json
```

`config list` is every setting with whether it is set and where from — a secret's value is
never in it. `doctor` groups each check by `section` with a `state` of ok, missing or failed.
`auth status` is every sign-in. If `doctor`'s `storage` check fails, an old `~/.jarvis` or
a `.env` in the checkout is still about: ask them to run `keryx migrate` (it stops the
service while it moves things), and carry on once it has.

Then tell them, in three or four sentences: what Keryx does with what you are about to
collect, that it goes to the realtime provider on every call, and that nothing is written
until they have seen it.

## 2. Set what you know

Plain settings go straight in: `uv run keryx config set KEY VALUE [KEY VALUE …]`. Only the
ones you were told or can see — a `PUBLIC_HOST` they named, `ALLOWED_CALLERS` they gave you
in E.164. A coding agent with no sign-in: `uv run keryx auth login claude` (or `codex`)
runs its own login, which opens a browser or prints a code — theirs to finish, not yours.

## 3. The plugins they want

Four voice tools are plugins, off until they are turned on: `send_to_slack` and
`check_email` (most people want both), `check_billing` and `cluster_stats`. Ask which they
want; do not turn one on unasked.

```bash
uv run keryx plugins --json            # each one: on or off, its files, why one is refused
uv run keryx plugins install check_billing --set monthly_budget=40
uv run keryx plugins hosts --json      # the ssh hosts cluster_stats could ask
uv run keryx plugins install cluster_stats --cluster HOST=PARTITION
```

A plugin's secret is never a `--set`: Slack's bot token and the billing admin keys are
`keryx config set KEY --stdin` (theirs to type, through `keryx setup` if you do not have
it), and email needs `keryx auth login gmail` (the sign-ins step of the instructions). Offer
`cluster_stats` only a host whose `control_master` is true — Keryx never opens a
connection of its own — and ask them which partition its GPUs are in rather than guessing.
If `doctor` names retired settings, `uv run keryx plugins install --from-settings` moves
them. Every other setting of a plugin is in the TOML file `keryx plugins` names.

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
  | uv run keryx memory seed --file - --json
```

Exit 1 means there was already a memory there, and that is theirs to decide about.

## 8. Hand over

`uv run keryx doctor --json` once more, then tell them in a few lines:

- **Run `uv run keryx setup`.** It walks only what is left, and what is left is theirs:
  the secrets you did not have, browser sign-ins, **the PIN** (nothing of theirs is read out
  on the phone and nothing can be dispatched until one exists; the first call can set one
  instead, and whoever calls first sets it), and approving where the Twilio number points.
- `uv run keryx memory` shows what it remembers; it is plain markdown they can edit.
- After every authorized call, Keryx folds that call into the memory itself. This was the
  first draft, not the last word.
