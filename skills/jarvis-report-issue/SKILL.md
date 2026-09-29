---
name: jarvis-report-issue
description: >-
  File a bug report or a feature request for Jarvis itself — something it got wrong on a
  call, something broken, or something they wish it could do — as a GitHub issue on
  Jarvis's own repository, after a short look for context. Use when the owner asks to
  report, file, log or suggest something about Jarvis, not when they want it fixed or built.
---

# Reporting a bug in Jarvis, or asking for a feature

The owner has told Jarvis, out loud, that something about Jarvis is wrong or missing, and
asked for it to be passed on. It is one of two things, and the first job is to say which:

- **A bug report:** something Jarvis did wrong or failed at — misheard, cut them off, said
  a thing twice, a tool that broke.
- **A feature request:** something Jarvis cannot do yet and they wish it could — "you
  should be able to…", "it would be nice if Jarvis…".

Your job is to turn it into an issue a maintainer can act on: for a bug, what happened,
where it probably lives, and the evidence; for a feature, what they want to be able to do,
why, and where it might go. It is **not** to find the bug, fix it, or build the feature. The owner pays for every token you spend, and a bug in Jarvis is
not theirs to pay for — so the look around is short, and the issue says so.

Your instructions name the repository to file on, the checkout Jarvis runs from, its logs,
its command (written `python -m jarvis` below; use the exact one you were given), and the
transcript of the call they asked from, when there is one.

## The budget

About **ten tool calls** of looking, then write. Stop sooner as soon as you can say where
it probably lives; a hedged guess is enough.

- **Fine:** reading the end of the logs, grepping and reading a function or two in the
  checkout, `git -C <checkout> describe --always --dirty`, `python -m jarvis doctor --json`,
  `python -m jarvis tasks show <id>` for a task the complaint is about,
  `python -m jarvis restart --status`, `python -m jarvis config get <KEY>` (it never prints
  a secret), and `gh` to look for duplicates.
- **Not:** running the tests, reproducing it, a debugger, bisecting, writing a fix or a
  patch, a branch, a pull request, or any edit to the checkout. If you think you see the
  fix, one sentence in the issue saying where to look is the whole of it.

## 1. Is it an issue at all?

Three reasons not to file, each cheap to check:

- **It is this machine's setup** (a bug only). Run `python -m jarvis doctor --json`. If a check that is
  `missing` or `failed` explains it — a sign-in lapsed, a setting unset — it is not a bug.
  Do not file; say in your report what to fix and how.
- **It is a security problem** — a way past the PIN, a secret or a phone number showing up
  where it should not, a tool call approved without a key being pressed, a caller hearing
  what they should not. **Never file one publicly.** Do not file; your report says it needs
  a private report at `https://github.com/<repo>/security/advisories/new`, from their
  keyboard, and your spoken summary says that much and nothing of the details.
- **It is already filed.** Search with two or three words of the symptom or the wish:

      gh issue list --repo <repo> --state all --search "<words>" --limit 10

  An open issue that is clearly the same: do not open a second one. Add a comment with
  `gh issue comment` only if you have something it lacks — a new symptom, a newer
  version, another reason to want the feature — and report its number. A closed one, fixed by a commit this checkout does not
  have yet (`git -C <checkout> merge-base --is-ancestor <commit> HEAD` fails): do not
  file; say it is fixed in newer code and an update and restart would bring it in.

## 2. The short look

For a feature request it is shorter still: skip the log and the version, and spend the
look on whether Jarvis already has something close (a tool, a plugin, a setting — the
owner may simply not know it exists, in which case say so and do not file) and on where it
might go. `docs/tools.md` in the checkout says what the voice tools are and how a new one
is added. A thing only this owner would want — a check of their own server, their own
account — is a voice tool of their own, not a feature for everyone: say so in your report
rather than filing it.

- **The version:** `git -C <checkout> describe --always --dirty`, and the commit's date.
- **The log:** `jarvis.log` in the logs directory. Look at the end, around the time it
  happened, for `ERROR`, `WARNING` and tracebacks. Keep at most twenty lines, only the
  ones that bear on it.
- **The call**, if you were given its transcript: read it to understand what actually
  happened — what Jarvis said, which tool it called, what came back. It is for your
  understanding only (see below).
- **Where it probably lives:** grep the checkout for what the symptom names — a tool's
  name, a sentence Jarvis said, an error message — and name one to three files or
  functions. `CLAUDE.md` in the checkout is its map. Link them as
  `https://github.com/<repo>/blob/<commit>/<path>#L<line>` when the commit is on the
  remote (`git -C <checkout> branch -r --contains <commit>` lists a branch); otherwise
  give the path and line.

## 3. What never goes in

The repository is public: an issue is read by strangers and indexed for good. Everything
you found on this machine is private until you have checked it.

- **No phone numbers**, whole or masked. **No PIN**, nor any run of digits that could be
  one. **No key, token, password or secret**, and no email address.
- **No names** — not the owner's, not anyone they mentioned — and nothing about their
  work: no project, repository, host, cluster, file or directory name of theirs. A path in
  the checkout is given relative to it; a path anywhere else is left out.
- **Nothing quoted** from a call, a task's description or report, or Jarvis's memory.
  Describe the pattern instead: "asked for a call-back; Jarvis confirmed it twice in two
  turns", never the words that were said.
- **Log lines** only from Jarvis's own log, only the ones that matter, with all of the
  above taken out. A traceback is fine once it is clean.
- `python -m jarvis doctor` output never prints a secret (it says "set" or "not set"), so
  it may go in as it is — trimmed to the checks that bear on it.

Before you post, read the whole body once more for exactly these. When in doubt, leave it
out: the maintainer can ask, and a published secret cannot be taken back.

## 4. The issue

Follow the repository's own templates, which are in the checkout:
`.github/ISSUE_TEMPLATE/bug_report.md` for something broken,
`.github/ISSUE_TEMPLATE/feature_request.md` for something missing. Use their headings, in
their order, and fill every one; the `<!-- -->` comments are instructions to you, not text
to keep. Take the label from the template's front matter, and pass it with `--label` only
if `gh label list --repo <repo>` has it.

- **The title** is the symptom, or the thing they want to do, as a maintainer would
  search for it, in under seventy characters: "Call-back confirmation is spoken twice",
  "Read a text message out loud on request" — not "Bug", "Idea" or "Jarvis is broken".
- For a bug, **Reproducing it** is what the owner was doing, as steps, and says plainly
  that you did not reproduce it; **Environment** is the operating system, the Python
  version, the version from above, and any setting that bears on it (the coding agent,
  the realtime model).
- For a feature, **What you want to be able to do** is ideally the sentence they would say
  on the phone, in your words rather than theirs; **Why it belongs in Jarvis** takes the
  template's own question seriously — a voice tool, or work a dispatched task can already
  do — and says which this is.
- **Add one section at the end, "Where it probably lives"** (for a feature, "Where it
  might go"): the files or functions you found, hedged — "probably", "the first place I
  would look". Never more certain than a ten-call look allows.
- **End with this line**, so nobody mistakes it for a full diagnosis:

      _Reported by voice through Jarvis; the look around was deliberately brief._

Write the body to a file in your working directory — never in the checkout — and file it:

    gh issue create --repo <repo> --title "<title>" --body-file <file> [--label bug|enhancement]

If `gh` is missing, not signed in, or the create fails, do not retry in a loop and do not
post any other way. Put the whole draft in your written report, and say in your spoken
summary that it is written but not filed, and the one thing that would fix it (for
instance `gh auth login`, at their keyboard).

Nothing is committed, so there is no `Jarvis-Task:` trailer to add, and nothing here needs
a `RESTART_REQUIRED:` line.

## 5. Your report

Written: the issue's URL (or the duplicate's, or why nothing was filed), its title, the
body exactly as filed, and what you looked at. Spoken: one or two sentences — that it is
filed, whether as a bug or a feature request, its number, and what it says in a phrase. No
URL, no file names.

    SPOKEN_SUMMARY: I filed it as issue forty-two, about the call-back being confirmed
    twice. The link is in the report.
