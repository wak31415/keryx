# Setting Jarvis up

You need Python 3.12, [uv](https://docs.astral.sh/uv/), an OpenAI API key with Realtime
access, and a coding agent to do the work: Claude Code or Codex, signed in with a
subscription or an API key ([agents](agents.md)). Phone calls work on macOS or Linux and need
Twilio and a tunnel (Cloudflare Tunnel or ngrok).

```bash
git clone https://github.com/wak31415/jarvis-voice-agent.git
cd jarvis-voice-agent
uv sync
uv run jarvis setup
```

`uv sync` installs both coding agents. To install only one, see
[agents](agents.md#coding-agents-claude-code-and-codex).

## What `jarvis setup` asks

It asks only for what is still missing, and saves as it goes:

- the OpenAI key, which it checks with OpenAI before keeping it;
- which coding agent does the work, and its sign-in (it skips an agent that is already
  signed in);
- your name, your numbers and the phone PIN;
- then the optional sections: Twilio, Google, Slack, billing, a first memory, short
  summaries of your projects, and the background service.

Run it again at any time. It walks what is left, or everything with `--all`.
`uv run jarvis doctor` says what is still missing.

**Having a coding agent set Jarvis up?** Point it at
`uv run jarvis setup --agent-instructions`. Every step is a `jarvis config`, `jarvis auth` or
`jarvis memory` command it can run, and it hands you `jarvis setup` for what only you can do.
Claude Code users can copy `skills/jarvis-setup` into `~/.claude/skills/` to get the same
thing as a skill.

## Phone

`jarvis setup`'s phone section does all of it. It checks your Twilio credentials and lists
your numbers, then walks you through the tunnel
([Cloudflare Tunnel](../src/jarvis/setup/guides/tunnel.md) on Linux, ngrok on macOS). Last,
it shows you both addresses and asks before pointing the number's webhooks at
`https://<PUBLIC_HOST>/twilio/voice` and `/twilio/status`. Step by step:
[Twilio](../src/jarvis/setup/guides/twilio.md).

Then run it:

- `scripts/dev.sh` starts the server and the tunnel in a terminal;
- `scripts/install-systemd.sh` (Linux) or `scripts/install-launchd.sh` (macOS) installs them
  as a background service. Setup offers this too.

Texting is off until you turn it on (`SMS_ENABLED`), since many Twilio accounts can't send
SMS in every region.

Saying "hey jarvis" to your Mac, with no phone involved, is in development on the
[`feat/local-wakeword`](https://github.com/wak31415/jarvis-voice-agent/tree/feat/local-wakeword)
branch.

## The PIN

If no PIN is set, the first call may key one in, once. It is then written to
`~/.config/jarvis/pin`, and nothing in Jarvis can change it
([SECURITY.md](../SECURITY.md#setting-the-first-pin-on-the-first-call)). Only `jarvis setup`
at your keyboard can replace it.

## Google

Google is optional and also set up from `jarvis setup`. You make one Google Cloud client of
your own ([the steps](../src/jarvis/setup/guides/google.md)). Then a read-only Gmail sign-in
lets Jarvis answer questions about your email on a call. A second, optional sign-in lets
agents send mail and manage your calendar. Codex needs that second one to use Gmail and
Calendar at all.

## Settings

`uv run jarvis config list` shows every setting and where its value came from.
`jarvis config set KEY VALUE` changes one. A secret goes in with `--stdin`, so it never lands
in your shell history. [`configuration.md`](configuration.md) describes every key.

Jarvis can change a few of its own settings when you ask on a call: its voice, how long it
waits before answering, which model does the work. It cannot touch anything else.
Credentials, who may call, the PIN and every other line of defence are protected.
`jarvis config lock KEY` and `unlock KEY` change which of the rest it may write.

## Where Jarvis keeps its files

Jarvis keeps its files where uv, gh and git keep theirs, on Linux and macOS alike, and
honours each `XDG_*_HOME`. `uv run jarvis config path` prints them all:

| Directory | What is in it |
|---|---|
| `~/.config/jarvis` (`JARVIS_HOME`) | `config.toml`, `secrets.toml`, the PIN, the Google client file |
| `~/.local/share/jarvis` (`DATA_DIR`) | tasks and their reports, call transcripts, the memory, sign-in tokens |
| `~/.local/state/jarvis` (`STATE_DIR`) | logs, the restart record, the approval bridge's socket |
| `~/.cache/jarvis` (`CACHE_DIR`) | what can be downloaded again |

Every directory is readable by you alone. `~/.config/jarvis` holds `secrets.toml` and `pin`,
so keep it out of a dotfiles repository.

## Upgrading from `~/.jarvis` or a `.env`

Jarvis no longer reads either, and `jarvis serve` will not start while either is still
there. `uv run jarvis migrate --dry-run` shows what would move. `uv run jarvis migrate`
moves it: it stops the service, re-renders it and the approval hook, and starts it again.
Nothing is deleted. The old directory is renamed `~/.jarvis.migrated-<date>` with whatever
was left in it.

## Costs

Voice calls incur OpenAI API charges. Coding-agent tasks count against your Claude or
ChatGPT subscription limits by default. They are billed per token instead if you set
`ANTHROPIC_API_KEY` or `CODEX_API_KEY`.
