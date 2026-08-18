# Jarvis

A voice agent server: call in over Twilio, or talk to a local wake-word ("hey
jarvis") mic/speaker device. Speech goes through the OpenAI Realtime API for
low-latency conversation, and longer-running work (research, coding,
multi-step tasks) is dispatched to Claude Agent SDK subagents that report
back by voice, SMS, or a call-back.

See `docs/superpowers/specs/2026-08-18-jarvis-voice-agent-design.md` for the
full design.

## Prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- An OpenAI API key (Realtime API access)
- An Anthropic API key (Claude Agent SDK subagents)
- A Twilio account + phone number (for the phone channel)

## Quick start

```bash
uv sync
cp .env.example .env   # fill in OPENAI_API_KEY, ANTHROPIC_API_KEY, Twilio creds, etc.
uv run jarvis download-models
uv run jarvis serve
```

## Development

```bash
uv run pytest -q
uv run ruff check src tests
```
