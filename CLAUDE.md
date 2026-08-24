# CLAUDE.md

Jarvis: a Twilio phone + local wake-word voice agent, backed by the OpenAI
Realtime API and Claude Agent SDK subagents.

## Commands

- Run tests: `uv run pytest -q`
- Lint: `uv run ruff check src tests`
- Run the CLI: `uv run jarvis --help`
- Check the machine's setup: `uv run jarvis doctor` (`--no-mic` where there is none)
- Run the agent: `uv run jarvis serve` (`--no-phone` / `--no-wakeword` /
  `--fake-agents` / `--host` / `--port`); `scripts/dev.sh` adds the Cloudflare tunnel
- Inspect tasks: `uv run jarvis tasks list [--status …] [--limit N]`,
  `uv run jarvis tasks show <id>`
- One-off setup: `uv run jarvis download-models`, `uv run jarvis setup-google`
- Background service: `scripts/install-systemd.sh [--uninstall]` on Linux,
  `scripts/install-launchd.sh [--uninstall]` on macOS

## Layout

Source lives under `src/jarvis/` (installable package, `src/` layout). Tests
live under `tests/`, mirroring the package structure. `cli.py` stays argument
parsing plus wiring: the `doctor` checks live in `jarvis/doctor.py` and the
Google OAuth bootstrap in `jarvis/google_setup.py`. Service templates are in
`ops/systemd/` (Linux) and `ops/launchd/` (macOS), rendered by the matching
`scripts/install-*.sh`; `scripts/lib.sh` holds what those scripts share.

## Platforms

macOS runs both channels; Linux runs the phone channel only, because openwakeword
needs `tflite-runtime`, which has no cp312 wheel. `sounddevice`, `openwakeword` and
`onnxruntime` are therefore `sys_platform == 'darwin'` dependencies and a Linux host
serves with `--no-wakeword` — one more reason every import of them stays lazy.

## Testing rule

No network or hardware access in tests. OpenAI, Twilio, sounddevice,
openwakeword, and the Claude Agent SDK are always accessed through an
injectable interface (a `Protocol`) with a fake/test double used in tests —
never the real network or hardware. Heavy/hardware imports (`sounddevice`,
`openwakeword`) must be guarded inside functions, not imported at module
scope, so the test suite can run on a machine with no mic.

## Working agreements

- Only scripts read the env file; never print or paste its contents.
- Spec §3.2 interface names and signatures stay stable (extra optional keyword
  arguments are fine). §3.3/§4 hold rulings: follow them, and amend the spec in a
  docs commit when one changes.
- Conventional commits (`feat:`/`fix:`/`chore:`/`docs:`) with the Co-Authored-By
  Claude trailer.
- Clean and minimal over clever; TDD, with `uv run pytest -q` and
  `uv run ruff check src tests` pristine before a commit.

## Reference docs

- Design spec: `docs/superpowers/specs/2026-08-18-jarvis-voice-agent-design.md`
- Implementation plan: `docs/superpowers/plans/2026-08-18-jarvis-voice-agent-plan.md`
