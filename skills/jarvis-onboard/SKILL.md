---
name: jarvis-onboard
description: >-
  Onboard a new Jarvis install: check its coding agent with `jarvis setup-agent`, interview
  the owner briefly, draft a .jarvis-brief.md for the projects they pick, propose additions
  to each agent's instructions file (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md), and seed the
  memory through `jarvis init`. Use when someone has just cloned or installed the Jarvis voice
  agent, says "set up Jarvis", "onboard me", or asks what Jarvis knows about them. Run once,
  at the keyboard.
---

# Onboarding a new Jarvis

Jarvis answers the phone knowing only what it is handed at the top of each call: a memory
document, the briefs the projects wrote about themselves, and the names of the projects a
task can be pointed at. On a fresh install all three are empty, and the fastest way to fill
them is one session at the keyboard with the person it will be working for.

You are that session. Interview them briefly, draft what you learned, show it, and write
only what they approve.

## The rules, and they are not preferences

- **Nothing is scanned, written or sent without them seeing it first.** Show the questions'
  answers back, show every brief, show every proposed edit. "I'll just have a quick look
  through your repositories" is exactly the thing this skill exists not to do.
- **The memory and the briefs are sent to the realtime provider on every call**, as part of
  the system prompt, re-sent each time. Say that to them in those words, before they answer
  anything, and leave out of both anything they would not send to OpenAI.
- **Never write `memory.md` yourself.** Jarvis owns that file and rewrites it after every
  call; a hand-written copy is overwritten or fights with the writer. Everything you agree
  on goes in through `jarvis init`.
- **Jarvis never edits `.env`, and neither do you.** Print the `OWNER_NAME=` line and let
  them paste it, and the `JARVIS_PIN=` line with it when `jarvis init` offers one.
- **Never choose a PIN for them, and never read `~/.jarvis/pin`.** `jarvis init` prints a
  suggested line of six random digits when the machine has none; those digits are theirs
  to keep or replace, and the only other way to set one is the first phone call, which
  they make themselves.
- **Never read or quote `.env`, a token, a key, or anything under `~/.jarvis/`** — the call
  transcripts there carry the spoken PIN.
- **Never scan a project they did not pick**, and never run `jarvis init --force` without
  asking: it replaces a memory that calls have already written.

## 0. Check the coding agent

Jarvis hands its work to a coding agent — Claude Code or Codex — and nothing else in this
session matters if that cannot run. From the repository:

```bash
uv run jarvis setup-agent --yes --json
```

It asks nothing and signs nothing in: it reports which agents are installed and signed in,
runs one real task through each chosen agent as a smoke test, and prints the
`AGENT_BACKEND=` / `AGENTS_ENABLED=` lines in `env_lines`. Exit 0 means every chosen agent
ran its task; exit 1 means one could not (the JSON says which, and why); exit 2 means the
command line was wrong. On exit 1, tell them what is missing and ask them to run
`uv run jarvis setup-agent` themselves — its logins open a browser or print a code, which is
theirs to do, not yours. Read `enabled` from the JSON: step 4 needs it. Show them the
`env_lines`; they add them to `.env` themselves.

## 1. See what is there

From the repository:

```bash
uv run jarvis init --yes --json
```

With no facts given this writes nothing at all: it prints what a call carries today — the
memory's size against the 4,000 characters a call reads, every project it found and which
of them wrote a brief, the brief total against its cap, the installed skills, and whether
`OWNER_NAME` is set. Exit 0 is that report; exit 1 means a memory was wanted and not
written; exit 2 means the command line was wrong. Read the `projects` list; you will need
it in step 3. The `pin` block says whether this machine has a PIN and where it came from,
never the digits: `"set": false` is the one thing they have to act on before the phone is
any use at all, and step 6 is where you say so.

Then tell them, in three or four sentences: what Jarvis does with what you are about to
collect, that it goes to the realtime provider on every call, and that nothing is written
until they have seen it.

## 2. Interview, briefly

Five questions, and stop:

1. What should Jarvis call them?
2. What do they work on — the one-paragraph version?
3. Which machines do they use, and where do their repositories live?
4. How do they like to be answered: the short answer, or the reasoning with it?
5. What is worth a phone call when a job finishes, and what can wait?

Keep their own words. Do not expand an answer into three facts, and do not invent a sixth
question because the fifth was interesting.

## 3. The projects they pick

Show them the project names from step 1 — the list, not the contents — and ask which ones
matter enough for Jarvis to know about. Draft a brief only for those, and only after they
have seen the list.

For each one, read its `README.md` and `CLAUDE.md` and draft a `.jarvis-brief.md` at the
project root:

- Written to be **heard**, not read: what the project is, what state it is in, and what the
  words in it mean out loud. No build commands, no directory trees, no code fences.
- At most **1,500 characters**. All the briefs together are capped at 6,000, and one past
  that is dropped whole from the prompt rather than truncated.
- A repository's `CLAUDE.md` is the wrong thing to paste in: it is thousands of tokens of
  build detail the subagent reads for itself, and it drowns a receptionist's prompt.

Show each draft in full and write it only once they say yes. A project they would rather
leave undescribed is a perfectly good answer.

## 4. Propose the subagents' own map

The subagents are ordinary sessions of whichever agent runs them, so they read that agent's
own instructions file like any other session: `~/.claude/CLAUDE.md` for Claude,
`~/.codex/AGENTS.md` for Codex (step 0's JSON names each one as `instructions_file`). That
file, not Jarvis's memory, is where "my repositories live under ~/code" and "the cluster is
reached with this script" belong. Draft the lines that are missing from their answers in
step 2, for each agent in `enabled` — the same lines in each, so the agents agree. Show
every edit before you make it, and append rather than rewrite.

## 5. Seed the memory

Turn their answers into short standing facts, one per line, and show them the list. Then,
from the repository:

```bash
printf '%s\n' \
  'Goes by Ada; prefers the short answer first.' \
  'Works on the orchard sensor rig and a weather dashboard.' \
  'Ring for anything that fails; everything else can wait for the next call.' \
  | uv run jarvis init --from - --yes --name 'Ada'
```

`jarvis init --from - --yes` reads one fact per line from stdin, writes `memory.md` and
nothing else, and prints the lines for them to add to `.env` — the `OWNER_NAME=` one, and a
suggested `JARVIS_PIN=` where the machine has no PIN. It never edits .env itself. Add `--json` if you want the result as a document; exit 1 means there was
already a memory there, and that is theirs to decide about, not yours.

## 6. Hand over

Tell them, in a few lines:

- What the first call will be like: with a memory now seeded it opens as an ordinary call.
  Had they skipped this, Jarvis would have spent the first call getting to know them
  instead — and it still will if the memory is empty.
- **The PIN, if `jarvis init` printed a line for one.** Nothing of theirs is read out on
  the phone and nothing can be dispatched until a PIN exists. Two ways, and they pick: paste
  the suggested `JARVIS_PIN=` line (or digits of their own) into `.env`, which is the
  permanent home; or let the **first call** set one — it asks for six to eight digits and
  hash, twice, and that is the PIN from then on. Say the second half of that plainly: the
  first call to reach Jarvis is the one that sets it, and once set nothing in Jarvis can
  change it — only they can, at the keyboard. `SECURITY.md` has the reasoning.
- `uv run jarvis memory` shows what it remembers, `uv run jarvis doctor` says what is still
  missing (including which source the PIN came from), and the memory is plain markdown they
  can edit by hand.
- After every authorized call, Jarvis folds that call into the memory itself. This was the
  first draft, not the last word.
