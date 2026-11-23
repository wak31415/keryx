# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [semantic versioning](https://semver.org/) over the command line and the
settings: a removed or renamed setting or command is a major bump, or a minor one while
the version starts with 0.

## [Unreleased]

### Added

- **Write back on Slack.** Your Slack app now has an access level, chosen before the app
  is created: `announce` (as before: Keryx posts and reads nothing), `converse`, `read`
  or `write`. At `converse`, every message you send the app itself is a request,
  with no mention needed: a new message starts a task, and a reply under a task's report
  continues that task. Nobody else's message does anything. Anywhere else Keryx acts
  only on a message of yours that mentions it. Keryx marks a message it has acted on with 👀. None of this needs the phone PIN, and
  `SECURITY.md` describes the trade; a reply that re-runs a task counts against the daily
  task cap. It listens over Socket Mode, so nothing new is exposed to the internet.
- **Your Slack app carries your assistant's name.** The app the link creates is called
  Lyra, Jarvis or whatever `ASSISTANT_NAME` says (fitted to what Slack takes, and Keryx
  when nothing usable is left), and everything that tells you what to type follows it:
  `@Lyra`, not `@Keryx`. What the app posts about itself in Slack no longer says "Keryx".
  An app made earlier keeps its name until its manifest is saved again, which `keryx auth
  login slack` prints with the steps.
- **An icon for your Slack app.** Keryx ships its mark as a PNG, and the connect steps
  end with an optional one that says where the file is and where to upload it. The
  app's manifest also sets a background colour, the mark's violet.
- **Ask from any conversation, at `read`.** Add the app to a channel and mention it there.
  It reacts to your message and answers in that thread, where the others in the
  conversation read the answer too. Ask in your direct messages with the app for anything
  you would not say in front of them.
- **Your Slack on a call: the `slack_read` plugin.** One tool whose arguments are filters
  that combine: a conversation (a channel's or a person's name), a thread, words to look
  for, who wrote it, what is waiting on you or what mentions you, and how many days back,
  with an optional question about what it found. "Anything waiting for me on Slack?",
  "what did Sam say about the budget?" and "read me that thread" are each answered in a
  few seconds. What is waiting is a best effort, since Slack has no such list, and it
  says so, and says when part of the check failed. A name that could mean several things
  is asked about, never guessed. It needs the PIN and a workspace at `read`, only ever
  reads, and only when you ask.
- **A fourth Slack level, `write`, and the `slack_message` plugin.** At `write` Keryx can
  send one message as you: to a person, a channel, or as a reply in a thread. On a call
  it never sends on the first ask: it says who the message goes to and the exact words,
  and sends only after you say yes. It cannot edit, delete, upload or notify a whole
  channel. A coding agent working on your request can send one too, and nothing asks you
  first then: `SECURITY.md` says plainly what that risk is. Lowering the level to `read`
  removes the capability at once.
- **`keryx plugins install slack_report --set`** takes where reports go (`channel_id`)
  and the MCP server, and refuses the access level, the owner and what Slack said of the
  workspace: those are settled by `keryx auth login slack`, which checks the tokens.
- **`send_to_slack` is now `slack_report`.** Nothing to do: an installed plugin keeps
  loading from the files it has and is offered under the new name. `keryx plugins install
  slack_report` renames those files if you want them renamed.
- **`keryx slack read|conversations|members|report|message`** are how a coding agent uses
  Slack for a request that is about it. `read` takes the same filters as the voice tool
  (plus `--limit` and `--before` to read further back) and prints what it read as fenced
  context; `report` posts to you as Keryx, and inside a task only to that task's own
  thread or to where your reports go; `message` sends as you at `write`, in two runs:
  the first prints who a name is and the id to send to, and only the second, with
  `--confirm` and that id, sends. Your user token is never handed to the agent.
- **What is read from Slack is treated as other people's words, never as instructions.**
  Only your own request is acted on. Everything read is wrapped, attributed to who wrote
  it, and kept apart from what you asked, as a guard against a message that tries to give
  Keryx orders. `SECURITY.md` says what that does and does not protect against.
- **Connecting Slack is one command at a terminal.** `keryx auth login slack` prints the
  steps and then waits: it asks for each token the level needs, hidden, keeps one it
  already has when you press Enter, and asks again for one that is the wrong kind or
  that Slack refuses. Ctrl-C saves nothing. `--stdin` is still how a script or a coding
  agent hands tokens over, and without a terminal the command prints the steps and ends.
- **`keryx auth login slack --access LEVEL`** says in plain words what the level lets
  Keryx see before it prints the link, takes the level's tokens at a hidden prompt (or
  on standard input, one to a line), and checks each before saving anything. Raising the level of an app you already
  have prints the manifest to paste into it, never a second app. A lower level is applied
  at once and forgets the tokens it no longer uses. `keryx setup` asks the same question
  first and saves nothing until every token has been checked. Whose messages are acted
  on is settled when you connect and nothing changes it afterwards: where reports are
  sent never does, and connecting as somebody else is a logout and a new login.
- **Connecting Slack takes your user token at every level, and no address.** A bot token
  does not say who installed the app; a user token does. So `keryx auth login slack` now
  takes the bot token and your user token (plus the app-level token from `converse`),
  Slack says which member you are, and reports go to you. `--email` is gone, and so is
  the question about where to send. Below `read` the user token has one permission
  (`emoji:read`) and is used only to ask Slack whose it is. The bot no longer asks for
  `users:read` or `users:read.email`. A workspace connected before this keeps working
  untouched; the next login shows how to update the app you have. To send reports
  somewhere else: `keryx plugins install slack_report --set channel_id=ID`.
- **`keryx auth logout slack [--workspace NAME]`** forgets a Slack workspace's tokens and
  settings.
- **More than one Slack workspace.** `keryx auth login slack --workspace NAME` connects
  another, with its own app, level and tokens. Reports and alerts keep going to your first
  workspace, `slack_report` can be told which one to use, and a task asked for in one
  workspace is answered there and nowhere else. `keryx doctor` and `keryx auth status`
  list each workspace with its level and anything that level still lacks.
- **A task's reports stay in one Slack thread.** The first report is a message of its own;
  later reports of the same task are replies under it, also shown in the conversation,
  and a long report is one thread.

- **One Google sign-in for everything: `keryx auth login google`.** It connects Gmail,
  Calendar, Drive, Docs, Sheets and Slides at once, for the coding agents and for email
  answers on a call, and `--services gmail,calendar` chooses fewer. With a browser on the
  same machine it is one command and no pasting: Keryx opens Google's screen and takes the
  answer itself. With `--headless`, or over ssh, it prints a link for any device and asks
  for the address the browser lands on. What you leave unticked on Google's screen is
  simply not connected, and the command says which services are.
- **`keryx auth logout google`** revokes Keryx's access at Google and deletes the sign-in
  from this machine.
- **Your calendar on the call: the `check_calendar` plugin.** "What's on tomorrow", "what's
  my next meeting", "when is the dentist" are answered in a second or two from Google
  Calendar itself, with no task dispatched. It reads every calendar you have ticked in
  Google Calendar, in your calendar's time zone, and needs the PIN.
  `keryx plugins install check_calendar`, or `keryx setup`, which ticks it the first time
  through once Calendar is connected.
- **`add_calendar_event`, a plugin of its own**, so reading your calendar can be on without
  anything on the phone being able to write to it. One event on your own calendar, timed or
  all day, with nobody invited; the assistant says back what it created. Anything more
  (guests, repeats, changes) is still a task for a coding agent.
- **Your documents on the call: the `find_document` plugin.** "Find the doc about the
  offsite" searches Google Drive by name and by text. "What does my planning doc say about
  hiring" also reads the best match and answers from it in a few seconds, with one model
  call, the way `check_email` does for mail. Docs, Sheets, Slides and text files can be
  read; a PDF is found and said to need a task. It needs the PIN, Drive in your Google
  sign-in and the `claude` extra.
- Agents get Drive, Docs, Sheets and Slides as well as Gmail and Calendar, and both Claude
  and Codex get them the same way.
- When a release ships Keryx's own Google app, signing in needs no Google Cloud project.
  Until then you make a client of your own once, and the guide in `keryx setup` now starts
  with Google's shorter Personal mode.

### Changed

- `keryx auth login gmail` and `keryx auth login google-workspace` run the one Google
  sign-in. Both names are accepted for one more release, and `keryx auth status --json`
  still lists them beside the new `google`.
- An install that already signed in keeps working and needs nothing done. Signing in again
  with `keryx auth login google` replaces the two older sign-ins with one.
- If you made your own Google Cloud client with the earlier guide, its project has only the
  Gmail and Calendar APIs enabled. After signing in again, enable Drive, Docs, Sheets and
  Slides there too (the links are in the guide `keryx setup` shows, and the sign-in names
  it), or those tools answer "API not enabled".
- The assistant answers a quick question about your mail, your calendar or a document
  itself when the plugin that reads it is on, and no longer hands it to a coding agent
  first. Replying, changing an event and editing a file are still tasks.
- A sign-in turns the agents' Google tools on the first time only. If you have set
  `GOOGLE_WORKSPACE_MCP` to false, signing in again leaves it off.
- A coding agent finishing a Google sign-in gives the address on standard input
  (`--callback-url -`), not in the command, where its one-time code could be read.
- Signing in again adds to what is connected. `keryx auth login google --services
  calendar` keeps Gmail and the rest and adds Calendar, and says so.
- Signing in as a different Google account from the one you signed in as before is
  refused unless you say you mean it: `keryx setup` asks, and the command line takes
  `--account ADDRESS`.
- The agents' Google tools are attached only once you have signed in, and they can no
  longer open a sign-in of their own in the middle of a task.
- Turning `check_email` off no longer offers to delete the Google sign-in, because the
  agents use it too.
- `keryx doctor` has one **Google** check in place of "Google for agents": who is signed
  in, with whose client, and which services are connected.

### Fixed

- `keryx.log` no longer records the address of every Google request. The HTTP client
  logged each one in full at the default log level, which for `check_email` meant message
  and thread ids and the search that was run. Requests are still counted in Keryx's own
  lines.
- With `LOG_LEVEL=DEBUG`, `keryx.log` recorded the OpenAI API key each time a call opened,
  and parts of every message to and from the voice model, which can include a PIN. The
  websocket library wrote both, and it is now held to warnings at every log level. If you
  have run at `DEBUG`, delete those logs and replace the key.
- With `LOG_LEVEL=DEBUG`, the service's own output (`keryx.err.log` beside `keryx.log`, or
  the terminal) recorded each keypad digit as it was pressed, which is the PIN, and the
  number of whoever called. The phone server is no longer run below `INFO`, whatever
  `LOG_LEVEL` says. If you have run at `DEBUG`, delete that log too; `keryx setup` sets a
  new PIN.
- The request log no longer records the token of a report link. Each time a
  `/reports/…` link was opened, its whole address went into `keryx.out.log`, token
  included, at the default log level. The line is still written, without what follows
  the `?`.
- An email written for the purpose could stall the whole service while `check_email` read
  its body: a body of nothing but `<`, or of script tags that never close, took time that
  grew with the square of its length. Reading a body now takes time in proportion to it.
- `keryx auth status` reported Gmail as missing however it was signed in.
- `--headless` was ignored by both Google sign-ins.

## [0.2.0] - 2026-11-09

Keryx installs with one line and no clone, and every command starts with `keryx`. An
installed service needs nothing done: the new commands write the same units the scripts did.

### Added

- **A one-line install:**
  `curl -fsSL https://raw.githubusercontent.com/wak31415/keryx/main/install.sh | sh`. It
  installs the `keryx` command with both coding agents and starts `keryx setup`. It needs no
  Python, uv or clone: it brings its own. Keryx is published on PyPI as `keryx-voice`, so a
  tool of your own can have Keryx in its editor with `pip install keryx-voice`.
- **`keryx service install`** installs and starts the background service with its tunnel
  (systemd on Linux, launchd on macOS), and **`keryx service uninstall`** removes them. The
  last section of `keryx setup` runs it.
- **`keryx upgrade`** updates an installed Keryx and restarts the service onto the new
  version. It checks that the new version starts before it restarts anything. You can also
  ask for an upgrade on a call: Keryx installs it while you talk, restarts once the call
  ends, and rings you back to say which version it is on. If nothing newer exists, it
  tells you at once.
- **`keryx approvals --install-hook`** installs the approval bridge's hook into Claude Code,
  and `--uninstall-hook` takes it out.

### Removed

- `scripts/install-systemd.sh`, `scripts/install-launchd.sh`,
  `scripts/install-claude-hook.sh` and `scripts/lib.sh`, replaced by the commands above.
  `scripts/dev.sh` stays, for working on Keryx from a clone.
- `keryx migrate`, `keryx config import-env` and `keryx plugins install --from-settings`,
  which moved settings from before 0.1.0, and the refusal to start beside a `.env` in the
  working directory.
- `FAKE_AGENTS`, the old name of `DEMO_MODE`, and the hidden `serve` options
  `--fake-agents` and `--no-wakeword`.

## [0.1.2] - 2026-11-04

Fixes to how results reach you, finished tasks' reports on Slack, and updated dependencies.
Nothing to change on your side: no setting or command was renamed or removed.

### Security

- **certifi 2026.10.18 drops the emSign root certificates** whose intermediate CA keys were
  compromised (GHSA-6q48-6c7w-9gw2, critical). It is the certificate bundle that the HTTP
  libraries under the OpenAI and Twilio clients trust by default.

### Changed

- **A finished task's whole report is posted to your Slack DM.** While the `send_to_slack`
  plugin is on, each report is posted under a line naming the task, with its headings,
  tables and links rendered and no link previews. The request and the spoken summary are
  left out, and a long report is no longer split mid-list. On a call, questions about a
  finished task are answered from its report instead of starting new work. Messages sent
  with `send_to_slack` render Markdown the same way.
- **Dependencies are updated.** The Codex agent runs on `openai-codex` 0.163.0 and its
  bundled `codex` CLI. Twilio 9.12.0, websockets 17.2 and numpy 2.5.4 are updated
  too.

### Fixed

- **No call-back on top of the call you are already on.** If a task you asked to be called
  back about finished while you had rung Keryx again, your phone rang mid-call with the same
  result. The call-back now waits for that call to end. It does not ring at all if you gave
  the PIN on that call after hearing the result, or if the result was marked as heard.
- **A result read out mid-call is recorded as heard.** It used to come back at the top of
  the next call. The announcement replaced the assistant's instructions for that turn,
  including the one that says to record it; it no longer does, and it now asks for the
  record in the same turn.
- **A result you heard before giving the PIN is not told to you again after it.** Once the
  PIN is in, it is recorded as heard instead of being repeated.
- **Asking about your email no longer misses the thread you meant because of one broad
  word.** A search with alternatives now looks for each one separately, so newer mail
  matching a common word cannot push out the thread you asked about, and the summary sees
  more threads. When nothing matches, the assistant says what it searched for, so you can
  correct a misheard name or word.
- **The assistant asks when part of a request comes through garbled.** If a phrase
  transcribes as nonsense, it asks one short question about that part instead of acting on
  it.
- **The restart watchdog starts on its own instead of through the whole app.** A code
  change that breaks the import no longer silences the alert that Keryx never came back.
- **The installers work from a path with a space or a `%` in it.** The systemd units quote
  the checkout and the `uv` and `cloudflared` paths, and the launchd plists escape every
  value, so a checkout such as `~/My Projects/keryx` no longer leaves a unit that will not
  start.

## [0.1.1] - 2026-10-19

Fixes from an audit of 0.1.0, and updated dependencies. Nothing to change on your side: no
setting or command was renamed or removed.

### Security

- **Dependencies with published advisories are updated.** aiohttp 3.14.4, multidict 6.9.1,
  mcp 1.30.0, PyJWT 2.15.0 and urllib3 2.8.0 each fix one or more advisories. Keryx does
  not import any of them; they come in through Twilio and the Claude Agent SDK.

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

### Changed

- **The coding agents' SDKs are updated.** The Codex agent runs on `openai-codex` 0.158.0
  and its bundled `codex` CLI, and the Claude agent on `claude-agent-sdk` 0.2.160 and its
  bundled Claude Code CLI.

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

[Unreleased]: https://github.com/wak31415/keryx/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/wak31415/keryx/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/wak31415/keryx/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/wak31415/keryx/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/wak31415/keryx/releases/tag/v0.1.0
