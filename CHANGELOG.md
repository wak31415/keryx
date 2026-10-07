# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [semantic versioning](https://semver.org/) over the command line and the
settings: a removed or renamed setting or command is a major bump.

## [Unreleased]

### Added

- **Local models.** The work and the voice can run on your own hardware, or on a server you
  can reach. `keryx setup` has a Local models section that shows what your machine can hold,
  recommends an open model that fits, downloads it from Hugging Face, and runs it under
  llama.cpp (or Ollama), with a local voice beside it if you want one. `keryx models
  list|pull|serve` does the same from the command line. See
  [docs/local-models.md](docs/local-models.md).
- **A third agent, `local`**: your model, run inside Claude Code or Codex
  (`LOCAL_AGENT_BASE_URL`, `LOCAL_AGENT_MODEL`, `LOCAL_AGENT_API`). Your Anthropic and OpenAI
  credentials are never sent to it.
- **A voice server of your own** (`VOICE_BASE_URL`): any server that speaks the Realtime
  protocol, such as Hugging Face's speech-to-speech. Keryx converts the phone's audio for it.
- **Web search without OpenAI** (`WEB_SEARCH`): SearXNG, Google through Gemini, or the public
  search engines through `ddgs`, as well as OpenAI. `auto`, the default, uses the first that
  is set up.

### Changed

- `OPENAI_API_KEY` is needed only for OpenAI's voice (and OpenAI's search, if you pick it).
- A result Keryx announces during a call is marked as told once the announcement starts
  playing, even if you interrupt it, so it does not come back at the top of your next call
  ([#82](https://github.com/wak31415/keryx/issues/82)).

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

[Unreleased]: https://github.com/wak31415/keryx/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/wak31415/keryx/releases/tag/v0.1.0
