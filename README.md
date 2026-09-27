<p align="center">
  <img src="docs/assets/jarvis-mark.svg" width="88" alt="">
</p>

<h1 align="center">Jarvis</h1>

<p align="center"><em>A personal voice agent that keeps working after you hang up.</em></p>

<p align="center">
  <a href="https://github.com/wak31415/jarvis-voice-agent/actions/workflows/ci.yml"><img src="https://github.com/wak31415/jarvis-voice-agent/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-Apache--2.0-6366f1.svg" alt="Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.12-6366f1.svg" alt="Python 3.12">
  <img src="https://img.shields.io/badge/coverage-96%25-22d3ee.svg" alt="Coverage 96%">
</p>

Start hours of work in a short call. Ask Jarvis to research a question, change code, or run
an experiment, then hang up. It keeps working, lets you check in or add instructions later,
and can call you back or text you the report when it's done.

Jarvis uses the OpenAI Realtime API for conversation and coding agents on your machine for
the work. Call it from a phone or a watch that can place calls, or say "hey jarvis" at your
Mac.

## What Jarvis adds

Jarvis turns the [Realtime API](https://developers.openai.com/api/docs/guides/realtime) into
a voice agent you can use across calls:

| Feature | Realtime API | ChatGPT Voice | Jarvis |
| --- | :---: | :---: | :---: |
| Live voice conversation and tools | ✅ | ✅ | ✅ Uses Realtime API |
| Phone calls, including from calling watches | 🟡 SIP setup | 🟡 1-800-CHATGPT, US/CA | ✅ |
| Email, calendar and Slack by voice | 🟡 via MCP | ✅ | ✅ |
| **Tasks that continue after you hang up** | — | ✅ in Work | ✅ |
| Status and follow-ups in a later call | — | 🟡 in the app | ✅ |
| SMS links to written reports | — | — | ✅ |
| Callbacks when you ask for one | — | — | ✅ |
| Pro-actively calls you when it needs input from you | — | — | ✅ |
| Ask it to add a feature during a call | — | — | ✅ |

For example:

> *"In the imaging project, run a learning-rate sweep on the microscopy images. Call me if
> you need a decision, and again when training finishes with a quick summary of the results."*

> *"Find the papers my group emailed this week, turn them into a reading-group presentation
> with polished animations and some discussion questions, and send it to me on Slack."*

> *"Add a voice command that checks whether my home server is up. Build it in the Jarvis
> project and run the tests."*

> *"One more thing: include GPU memory use in that sweep comparison."*

## A short call, a long task

This example shows a task continuing across calls: Jarvis asks one question, then calls
again with the training results.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/jarvis-call-flow-dark.svg">
  <img src="docs/assets/jarvis-call-flow-light.svg" alt="A call starts a tracked task that continues after you hang up and returns by SMS, callback, or a later call.">
</picture>

## Quick start

You need Python 3.12, [uv](https://docs.astral.sh/uv/), an OpenAI API key with Realtime
access, and a coding agent to do the work: Claude Code or Codex, signed in with a
subscription or an API key. The wake word runs on macOS; phone calls work on macOS or Linux
and also need Twilio and a Cloudflare tunnel.

```bash
git clone https://github.com/wak31415/jarvis-voice-agent.git
cd jarvis-voice-agent
uv sync                                        # every coding agent; for one: see below
cp .env.example .env
```

`uv sync` installs both coding agents. Each bundles a large CLI, so on a tight disk install
only the one you use: `uv sync --no-group agents --extra codex` (or `--extra claude`). With
pip, name it: `pip install '.[all]'`, `'.[claude]'` or `'.[codex]'` — plain `pip install .`
installs neither.

Add `OPENAI_API_KEY` to `.env`, then set up the coding agent. This finds what is installed,
runs the sign-in that is missing, proves it with one real task, and prints the lines to add
to `.env` (see [Choosing your coding agent](#choosing-your-coding-agent)):

```bash
uv run jarvis setup-agent
```

Check the rest of your setup with:

```bash
uv run jarvis doctor
```

Optionally, tell Jarvis about yourself before the first call with `uv run jarvis init`, or
let Claude Code interview you with the skill in `skills/jarvis-onboard` (copy it into
`~/.claude/skills/`). Otherwise the first call opens with a short introduction.

### Talk locally on a Mac

```bash
uv run jarvis download-models
uv run jarvis serve --no-phone
```

Say "hey jarvis" to start a session. Give your terminal microphone access in macOS System
Settings when prompted.

### Call Jarvis by phone

1. Add your Twilio credentials, phone number, allowed callers, PIN, and public hostname to
   `.env` (see [Configuration](#configuration)).
2. Install `cloudflared` and create a tunnel for a hostname in a Cloudflare-managed domain:

   ```bash
   cloudflared tunnel login
   cloudflared tunnel create jarvis
   cloudflared tunnel route dns jarvis jarvis.example.com
   ```

   Set `PUBLIC_HOST=jarvis.example.com` in `.env`, using your own hostname.
3. In Twilio, set the number's incoming voice webhook to `https://<PUBLIC_HOST>/twilio/voice`
   and its call-status webhook to `https://<PUBLIC_HOST>/twilio/status`. Use HTTP POST for
   both.
4. Run `uv run jarvis doctor --no-mic` on a machine without a microphone, then start the
   server and tunnel with `scripts/dev.sh`.

For a setup that starts automatically after a reboot, see the
[wiki](https://github.com/wak31415/jarvis-voice-agent/wiki).

## Choosing your coding agent

Jarvis hands its work to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or
[Codex](https://developers.openai.com/codex). `AGENT_BACKEND` picks the default; with both
enabled in `AGENTS_ENABLED`, you can say "have Codex do it" on a call. Either one signs in
three ways, and the first one set wins:

| Tier | Claude | Codex |
|---|---|---|
| API key (pay per token) | `ANTHROPIC_API_KEY` | `CODEX_API_KEY` |
| Headless subscription token | `CLAUDE_CODE_OAUTH_TOKEN` | `CODEX_ACCESS_TOKEN` |
| Stored subscription login | `claude` → `/login` | `codex login` |

`uv sync` installs both: each SDK bundles its own CLI (Codex's is about 350 MB), and
[Quick start](#quick-start) says how to install just one. Both do
the same work here: voice dispatch, follow-ups, progress, projects, Slack, and Gmail and
Calendar (for Codex through `GOOGLE_WORKSPACE_MCP` and `jarvis setup-google`), and both
record the tokens a task spent. A follow-up reaches Codex in the turn it is running; Claude
takes it when the turn ends. Claude also has a per-task dollar figure and cap. The approval
bridge stays Claude Code only. [`docs/agents.md`](docs/agents.md) has the full comparison.

## Answer Claude Code prompts by phone

If you use Claude Code on the same machine, Jarvis can ring you when a session on your
screen stops to ask you something and you haven't answered within five minutes. It reads
the question out, and you answer on the keypad. Install the hook once:

```bash
scripts/install-claude-hook.sh
```

It copies `scripts/claude_hooks/jarvis_approval.py` into `~/.claude/hooks/` and adds it to
`~/.claude/settings.json`, keeping a backup and any hooks you already have. Re-run it after
pulling changes to `scripts/claude_hooks/`; Jarvis ignores an outdated copy. Answering at
the keyboard always wins, and any failure leaves the prompt on your screen as usual.
`uv run jarvis approvals` shows what it has asked and `--disable` turns it off. Only
routine commands can be approved by phone; the
[wiki](https://github.com/wak31415/jarvis-voice-agent/wiki/The-Approval-Bridge) has the
details.

## Configuration

`.env.example` lists all available settings. For phone calls, set `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_NUMBER`, `ALLOWED_CALLERS`, `JARVIS_PIN`, and `PUBLIC_HOST`.
Use a 6–8 digit PIN and list allowed callers in E.164 format. With no PIN set, the first
call may key one in, once; it is then written to `~/.jarvis/pin` and nothing in Jarvis can
change it ([SECURITY.md](SECURITY.md#setting-the-first-pin-on-the-first-call)). You can also set `PROJECTS`
to give your repositories names you can say aloud. Texting is off until you set
`SMS_ENABLED=true`, since many Twilio accounts can't send SMS in every region.

Tasks run with your user account's access to files and the network. Keep the caller list
narrow and the PIN enabled. Read the wiki's
[security guidance](https://github.com/wak31415/jarvis-voice-agent/wiki/Security-Model)
before putting the phone channel online.

Voice calls incur OpenAI API charges. Coding-agent tasks count against your Claude or
ChatGPT subscription limits by default, or use token billing if you set `ANTHROPIC_API_KEY`
or `CODEX_API_KEY`.

## Extend Jarvis

You can ask Jarvis to add a feature while you're on the phone. For example:

> *"In the jarvis project, add a tool that tells me when the next train leaves my station."*

Jarvis turns the request into a task for a coding agent, which can update the code and run
the tests. When the change is ready, ask Jarvis to restart so the new tool becomes
available. You can also expand what tasks can do by adding skills or connected services for
the agent to use. [`docs/tools.md`](docs/tools.md) lists the tools the voice model has and
how to write one by hand; the [wiki](https://github.com/wak31415/jarvis-voice-agent/wiki)
has worked examples.

## More information

- [Wiki](https://github.com/wak31415/jarvis-voice-agent/wiki) for setup, troubleshooting,
  security, and examples.
- [Contributing guide](CONTRIBUTING.md) for development and pull requests.
- [Security policy](SECURITY.md) for reporting vulnerabilities.

Licensed under [Apache 2.0](LICENSE).
