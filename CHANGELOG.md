# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [semantic versioning](https://semver.org/) over the command line and the
settings: a removed or renamed setting or command is a major bump, or a minor one while
the version starts with 0.

## [Unreleased]

### Changed

- **`keryx auth login google` makes your Google client with you, in the same command.**
  With none on file, it says in a sentence why you need one, opens Google's Personal mode
  page (or prints it for a browser on another machine), shows the three clicks, and asks
  for the file it downloads or the Client ID: Enter takes the newest Desktop
  `client_secret_*.json` in `~/Downloads`, a folder takes the newest one in it, a Client ID
  is followed by its secret, asked for hidden, `paste` takes what is in the file, hidden,
  for a browser on another machine, and `skip` leaves it for later. Then it signs in as
  before. `keryx setup` asks the same. Without a terminal, `--client-file FILE` is still how
  the file is given, and the refusal names the page.
- **A Google client given at the sign-in replaces the one on file only once the sign-in
  with it works.** One Google refuses is thrown away, so the next `keryx auth login google`
  asks for the client again; after any other failure it is kept, and the next time Enter
  takes it. A client already on file is never touched by a failed attempt, and the message
  says how to give another.
- **A second way for an account that cannot create a Google Cloud project**, as many work
  and school accounts cannot. Type `other` at that question for the steps, each with its
  link: make the app with a personal Google account, turn on the six APIs in one click,
  publish it so sign-ins do not expire after seven days, download a Desktop client, and
  sign in with the work account past one "unverified app" warning. The guide replaces its
  old External-app steps with these.
- **While the browser is out, Keryx says what an organization's block means**: if Google
  says your organization blocks the app, only its administrators can allow it.

### Removed

- **Keryx will not ship a Google client of its own.** Google's API Terms say "Developer
  credentials may not be embedded in open source projects"
  ([section 4(b)](https://developers.google.com/terms)), so every owner signs in with a
  client they made. The code that waited for a packaged client, and the sentences about
  services "Keryx's Google app" did not offer yet, are gone, and the roadmap no longer
  waits on Google's verification.

## [0.3.0] - 2026-11-24

Keryx reaches your Google account and your Slack. One Google sign-in lets the assistant
answer from your mail, your calendar and your documents during the call, and gives the
coding agents Gmail, Calendar, Drive, Docs, Sheets and Slides. For now it needs a Google
OAuth client of your own, which `keryx setup` shows you how to make. Slack becomes an app
of your own, named after your assistant: it sends you reports and files, and you tick
whether it also takes requests you type to it, reads your Slack as you, and sends
messages as you. An existing install keeps working with nothing done. If you have ever
run Keryx with `LOG_LEVEL=DEBUG`, read Security below.

### Added

#### Google

- **One Google sign-in for everything: `keryx auth login google`.** It connects Gmail,
  Calendar, Drive, Docs, Sheets and Slides at once, for the voice tools and the coding
  agents alike, and `--services gmail,calendar` connects fewer. Signing in again adds to
  what is connected and ends nothing. With a browser on the same machine it is one command
  and no pasting: Keryx opens Google's screen and takes the answer itself. Over ssh, or
  with `--headless`, it prints a link for any device and asks for the address the browser
  lands on. What you leave unticked on Google's screen is simply not connected, and the
  command says which services are. A sign-in that comes back as a different Google
  account from the one saved is refused unless you say you mean it: `keryx setup` asks,
  and the command line takes `--account ADDRESS`.
- **It needs a Google OAuth client of your own, for now.** Keryx does not ship a Google
  app of its own yet: that waits on Google's verification. You make a client once, in a
  Google Cloud project of your own, and give Keryx the file Google downloads: `keryx setup`
  shows the steps and asks for the file, or run `keryx auth login google --client-file
  FILE`. The steps start with Google's Personal mode, a client only your own Google account
  can use, with nothing for Google to review and no sign-in that expires after seven days.
- **`keryx auth logout google`** revokes Keryx's access at Google and deletes the sign-in
  from this machine.
- **Your calendar on a call: the `check_calendar` plugin.** "What's on tomorrow", "what's
  my next meeting" and "when is the dentist" are answered in a second or two from Google
  Calendar itself, with no task. It reads every calendar you have ticked in Google
  Calendar, in your calendar's time zone, and needs the PIN.
- **`add_calendar_event`, a plugin of its own**, so reading your calendar can be on without
  anything on the phone being able to write to it. It puts one event on your own calendar,
  timed or all day, with nobody invited, and the assistant says back the weekday and the
  date it used. It never adds an event in the past, and a time with no morning or
  afternoon to it is asked about. If Google never confirms an event, it says so and does
  not try again. Guests, repeats and changes are still a task for a coding agent.
- **Your documents on a call: the `find_document` plugin.** "Find the doc about the
  offsite" searches Google Drive by name and by text. "What does my planning doc say about
  hiring" also reads the best match and answers from it in a few seconds, with one model
  call, as `check_email` does for mail. Docs, Sheets, Slides and text files can be read;
  a PDF is found and said to need a task. It needs the PIN, Drive in your Google sign-in
  and the `claude` extra.
- **The assistant answers a quick question about your mail, your calendar or a document
  itself** when the plugin that reads it is on, and no longer hands it to a coding agent.
  Replying, changing an event and editing a file are still tasks.
- **The coding agents get Drive, Docs, Sheets and Slides** as well as Gmail and Calendar,
  Claude and Codex the same way. Their Google tools are attached only once you have signed
  in, and can no longer start a sign-in of their own in the middle of a task. The first
  sign-in turns them on (`GOOGLE_WORKSPACE_MCP`); set it to false to keep the sign-in for
  the answers on a call only.
- `keryx doctor` has one **Google** check: who is signed in, with whose client, and which
  services are connected.

#### Slack

- **Your own Slack app, made from a link.** `keryx auth login slack`, or the Slack step in
  `keryx setup` → Plugins, first asks what Keryx may do in your Slack, then prints a link
  that creates an app for exactly that and the steps to follow in Slack's console, and
  waits while you paste each token at a hidden prompt. Every token is checked with Slack
  before anything is saved, and Ctrl-C saves nothing. Slack itself says which member you
  are, from your user token, so there is no address to give and no channel id to look
  up: reports go to you, and a test message shows they arrive. Who you are is settled
  then, and connecting as somebody else is `keryx auth logout slack` and a new login. To
  send reports to a channel instead, `keryx plugins install slack_report --set
  channel_id=ID`. The app is named after your assistant (Lyra unless you chose another
  name), and Keryx ships its mark as an icon you can upload to it.
- **You tick what Keryx may do there.** Sending you reports and files is always on, and
  reads nothing. Three more things are boxes of their own, in any combination: acting on
  what you type to the app, reading your Slack as you when you ask, and sending messages
  as you after a yes. Nothing is ticked for you. Run `keryx auth login slack` again to
  change them: ticking one more prints the manifest to paste into the app you already
  have, and unticking needs nothing from Slack and counts at once. The tokens it no longer
  uses are forgotten, its voice tool is turned off, and a running Keryx stops. Your user
  token is kept only while reading or sending is ticked. Unticking everything is the off
  switch, and `keryx auth logout slack` forgets the workspace.
- **Write back on Slack.** With acting on what you type ticked, every message you send the
  app is a request, with no mention needed: a new message starts a task, and a reply under
  a task's report continues that task. With reading ticked as well, you can also mention
  the app in a channel you have added it to, and it answers in that thread, where the
  others there read the answer too. Nobody else's message does anything. Keryx marks a
  message it has acted on with 👀 and answers in the thread you asked in. None of this
  needs the phone PIN, which [SECURITY.md](SECURITY.md) explains, and a reply that sets a
  task going again counts against the daily task cap. Keryx listens over Socket Mode, so
  nothing new is reachable from the internet; it starts listening when it starts, so run
  `keryx restart` after ticking it.
- **Your Slack on a call: the `slack_read` plugin**, on while reading is ticked. "Anything
  waiting for me on Slack?", "what did Sam say about the budget?", "what was the last
  thing Sam sent me?", "summarise the team channel" and "read me that thread" are
  answered in a few seconds, as a question about your email is. A list is the newest few
  messages, and a question is answered from up to forty. What is waiting on you is a best
  effort, and it says so. A name that could mean several people or channels is asked
  about, never guessed. It looks a week back unless you say otherwise, a month at most,
  and when it finds nothing it says how far back it looked. It needs the PIN and only ever
  reads, only when you ask.
- **Messages sent as you: the `slack_message` plugin**, on while sending is ticked. "Tell
  Sam I'm running late" sends one message as you, to a person, a channel, or as a reply in
  a thread. On a call it never sends on the first ask: you hear who it goes to and the
  exact words, and it sends only after you say yes. It cannot edit, delete, send a file or
  notify a whole channel. A coding agent working on your request can send one too, and
  nothing asks you first then: [SECURITY.md](SECURITY.md) says plainly what that risk is.
- **`keryx slack read|conversations|members|report|message`** are how a coding agent uses
  your Slack for a request that is about it. `read` takes the same filters as the voice
  tool and prints what it read as fenced context. `report` posts to you, and `--file PATH`
  sends files with it, so "send me the plot on Slack" arrives from your own app.
  `message` sends as you, in two runs: the first prints who a name is and the id to send
  to, and only the second, with `--confirm` and that id, sends. Your user token is never
  handed to the agent.
- **What is read from Slack is treated as other people's words, never as instructions.**
  Only your own request is acted on. Everything read is wrapped, attributed to who wrote
  it, and kept apart from what you asked, as a guard against a message that tries to give
  Keryx orders. `SECURITY.md` says what that does and does not protect against.
- **Without reading ticked, Keryx says that it cannot read your Slack.** Asked on a call
  what is new on Slack, it says in a sentence that reading is not switched on, rather than
  hand the question to a coding agent. An agent is told the same, and told not to read
  your Slack with any other Slack tool it has, which may be signed in to another
  workspace.
- **A task's reports stay in one Slack thread.** The first report is a message of its own,
  and later reports of the same task are replies under it.
- **More than one Slack workspace.** `keryx auth login slack --workspace NAME` connects
  another, with its own app, boxes and tokens. Reports and alerts keep going to your first
  workspace, `slack_report` can be told which one to use, and a task asked for in one
  workspace is answered there and nowhere else. `keryx doctor` and `keryx auth status`
  list each workspace with what is on and off there.
- **Without a terminal, `--allow` says what is ticked.** A script or a coding agent runs
  `keryx auth login slack --allow requests --allow read` (once for each of `requests`,
  `read` and `send`, or `--allow none` for reports and files alone): it prints the steps
  and ends, and `--stdin` with the same options takes the tokens on standard input. With
  nothing connected and no `--allow`, it prints the choices and guesses none.

### Changed

- **`send_to_slack` is now `slack_report`.** There is nothing to do: an installed plugin
  keeps loading from the files it has and is offered under the new name, and the old name
  is still accepted on the command line. `keryx plugins install slack_report` renames its
  files if you want them renamed.
- **Slack set up before this release keeps working untouched.** Run `keryx auth login slack`
  when you want more: it shows how to update the app you have (paste the manifest it
  prints, reinstall, and copy your user token). Sending a file takes the `files:write`
  permission, which an app made earlier may lack; until the same command has added it,
  text reports work as before and a file fails with a sentence that says what to do.
- **Once Keryx has a Slack app of its own, it is used for everything.** A Slack MCP server
  named in `slack_report.toml` is then no longer used: subagents send with
  `keryx slack report` and Codex is no longer handed the server. A machine with only the
  server, and no bot token, works as before. `keryx doctor` and `keryx plugins` say which
  is in use.
- **Task numbers are no longer said or posted.** On a call the assistant calls a task by
  what you asked for. A report posted to Slack is headed "Finished:" or "Failed:" and your
  request cut to a line, and a text, the restart confirmation and the restart alert name
  no number either. `keryx tasks`, a report's link and the `Keryx-Task:` commit trailer
  still carry it.
- **`check_email` answers with `claude-sonnet-5-5` by default** (it was `claude-opus-5-5`),
  and so does the new `slack_read`. A plugin already installed keeps the model in its
  file; `keryx plugins install check_email --set model=…` changes it.
- **An install that signed in to Google before keeps working**, and needs nothing done.
  Signing in again with `keryx auth login google` replaces the two older sign-ins with one.
  If you made your own Google Cloud client with the earlier guide, its project has only the
  Gmail and Calendar APIs enabled: enable Drive, Docs, Sheets and Slides there too (the
  links are in the guide `keryx setup` shows), or those tools answer "API not enabled".
- Turning `check_email` off no longer offers to delete the Google sign-in, because the
  agents use it too.

### Deprecated

- `keryx auth login gmail` and `keryx auth login google-workspace` run the one Google
  sign-in. Both names are accepted for one more release, and `keryx auth status --json`
  still lists them beside `google`.
- `keryx auth login google --callback-url ADDRESS`, with the address on the command line,
  is accepted for one more release. Give `--callback-url -` and the address on standard
  input instead, which keeps its one-time code off the command line.

### Security

- **With `LOG_LEVEL=DEBUG`, `keryx.log` recorded the OpenAI API key** each time a call
  opened, and parts of every message to and from the voice model, which can include a
  PIN. The websocket library wrote both, and it is now held to warnings at every log
  level. If you have run at `DEBUG`, delete those logs and replace the key.
- **With `LOG_LEVEL=DEBUG`, the service's own output** (`keryx.err.log` beside
  `keryx.log`, or the terminal) recorded each keypad digit as it was pressed, which is the
  PIN, and the number of whoever called. The phone server is no longer run below `INFO`,
  whatever `LOG_LEVEL` says. If you have run at `DEBUG`, delete that log too, and set a
  new PIN with `keryx setup`.
- **The request log no longer records the token of a report link.** Each time a
  `/reports/…` link was opened, its whole address went into `keryx.out.log`, token
  included, at the default log level. The line is still written, without what follows
  the `?`.
- **`keryx.log` no longer records the address of every Google request.** The HTTP client
  logged each one in full at the default log level, which for `check_email` meant message
  and thread ids and the search that was run. Requests are still counted in Keryx's own
  lines.
- **An email written for the purpose could stall the whole service** while `check_email`
  read its body: a body of nothing but `<`, or of script tags that never close, took time
  that grew with the square of its length. Reading a body now takes time in proportion to
  it.

### Fixed

- **A result is no longer read out to a call that has already dropped.** When the audio
  connection broke without closing, Keryx kept its side of the call open for some seconds
  after Twilio had ended it. A task that finished in that time was read out to nobody and
  recorded as heard, so you never heard it. Keryx now ends its side as soon as Twilio
  reports the call over, and the result waits for the next call or the call-back.
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

[Unreleased]: https://github.com/wak31415/keryx/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/wak31415/keryx/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/wak31415/keryx/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/wak31415/keryx/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/wak31415/keryx/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/wak31415/keryx/releases/tag/v0.1.0
