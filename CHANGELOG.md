# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [semantic versioning](https://semver.org/) over the command line and the
settings: a removed or renamed setting or command is a major bump.

## [Unreleased]

## [0.1.1] - 2026-10-18

Fixes from an audit of 0.1.0. Nothing to change on your side: no setting or command was
renamed or removed.

### Security

- **Typing your PIN on an approval call can no longer approve anything.** When Keryx called
  about a Claude Code prompt, a digit of your PIN typed after the keypad menu opened went to
  the menu, so the PIN's first "1" could approve the request. Digits now reach the menu only
  when no PIN entry is in progress, and the call no longer asks for the PIN first.
- **A line break in speech can no longer mark a call as authorized.** Transcript lines are
  written on one line, so nobody can make the assistant say the marker that `recall` reads.
- **Hook-framework files are no longer approvable by phone.** A write to
  `.pre-commit-config.yaml`, `.husky/` or any lefthook configuration is refused, as `.git`
  already was: the `git commit` a keypad may approve next would run what is in them.
- **The service can no longer re-enable approval calls or delete transcripts.**
  `keryx approvals --enable` and `keryx forget` now refuse when a subagent runs them.
  Anyone can still switch approvals off.
- **A PIN said in pairs is now redacted from transcripts.** "Forty-two forty-two
  forty-two" is replaced with `[PIN]` like single digits are. The transcript is also redacted
  again before the memory update reads it, which covers the lines spoken before a first PIN
  was set.

### Fixed

- **A follow-up's result is read out again.** A task re-run by `send_followup` is now owed
  a call-back and a place in the next call's digest, instead of keeping the old "told"
  stamp. Only a finished task can be marked as told.
- **A restart that waits too long now fails visibly.** It used to give up after 40 minutes
  and clear its record, so `keryx restart --status` read as if nothing had happened and the
  call-back never came. It now records the failure and tells you.
- **A crash no longer leaves a task "running" for ever.** At startup, Keryx marks any task
  that was still running when it stopped as failed ("Keryx stopped while this was
  running"), so it stops blocking restarts and follow-ups. You hear about it in your next
  call.
- **A database upgrade that is interrupted no longer stops Keryx from starting.** The
  `tasks.db` upgrade now runs in one transaction.
- **Two memory updates no longer overwrite each other.** When two calls end close
  together, the second update now waits for the first, so neither call drops out of the
  memory.
- **Anthropic spend for a workspace now counts only that workspace.** `check_billing` used
  to read the whole organization's spend and report it as the workspace's, against the
  workspace's budget.
- **The service can no longer lift a limit by setting it very high.** A limit it may tune,
  such as `SUBAGENT_TIMEOUT_S`, is now refused above ten times its default. You can still
  set any value yourself.

## [0.1.0] - 2026-09-30

The first public release. You call a phone number and talk to your voice assistant, Lyra
by default. Keryx is the service behind it: it hands the work you ask for to a coding agent
on your machine, keeps that work running after you hang up, remembers what happened between
calls, and calls you when there's news.

### Added

- **A voice assistant you can call.** A Twilio number puts you through to an assistant that
  runs on the OpenAI Realtime API. It's called Lyra unless you choose another name, and
  Jarvis is a second built-in persona with its own voice. It answers small questions itself,
  with a web search when it needs one.
- **Tasks that keep running after you hang up.** Anything larger becomes a task for Claude
  Code or Codex, in the project you name. In a later call, you can ask how a task is going,
  add instructions, or cancel it. If both agents are set up, you can say which one to use.
- **Call-backs.** When a task finishes, or needs a decision from you, Keryx calls you and the
  assistant tells you the result. If your Twilio account can send SMS, you can turn on texts
  with a link to the written report. Texting is off by default.
- **Continuity between calls.** Each call starts with the results you haven't heard yet and
  what the assistant remembers about you. Keryx updates that memory after every call in
  which you gave the PIN. With the PIN, you can also ask about anything said in an earlier
  call. Your first call is a short introduction, so the assistant can learn what you work on
  and how you like to be answered.
- **Security for a phone line.** Keryx answers only your own numbers. A PIN of six to eight
  digits guards everything that acts on your behalf, and wrong guesses count across calls
  toward a lockout. You can set the PIN on the keypad during your first call. A call that
  Keryx places to your own phone can answer the question it's calling about without the
  PIN. [SECURITY.md](SECURITY.md) has the threat model.
- **The approval bridge.** If a Claude Code session on your screen stops to ask for
  permission and you don't answer within five minutes, Keryx calls you, and you answer on
  the keypad. Only routine, reversible commands can be approved by phone.
- **Keryx can grow during a call.** Ask for a new voice command, and a coding agent writes it
  as a tool of your own, ready on the next call without a restart. A change to Keryx itself
  ends with a restart and a call to confirm that the new code is running. If it doesn't
  start, a watchdog calls you with a plain alert.
- **Plugins.** Slack, email, billing, and Slurm cluster status are optional voice tools
  that you turn on in `keryx setup` or with `keryx plugins install`.
- **Bug reports and feature requests by voice.** If you turn it on, "that's a bug, report
  it" files a GitHub issue on Keryx's repository with nothing personal in it. It's off by
  default, and only you can turn it on.
- **Setup and upkeep from the command line.** `keryx setup` is a wizard that asks only for
  what's still missing, and `keryx doctor` checks the whole setup. `keryx config`,
  `keryx auth`, and `keryx tasks` cover settings, sign-ins, and what each task did and cost.
  `keryx forget` and two retention settings delete old transcripts and tasks.
- **A demo mode.** `keryx serve --demo` lets you try the phone before a coding agent is set
  up. Every task comes back with a sample answer, and no agent tokens are spent.
- **Linux and macOS.** Keryx runs as a systemd or launchd service, keeps its files in the
  standard XDG directories, and stores secrets in files only you can read.

[Unreleased]: https://github.com/wak31415/keryx/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/wak31415/keryx/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/wak31415/keryx/releases/tag/v0.1.0
