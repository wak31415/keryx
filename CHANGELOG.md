# Changelog

Notable changes, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [semantic versioning](https://semver.org/) over the CLI and `.env`
surface — a removed or renamed setting or command is a major bump.

## [Unreleased]

### Added

- A licence (Apache-2.0), CI on Linux and macOS, `SECURITY.md`, `CONTRIBUTING.md`, a code
  of conduct, issue and pull-request templates, and Dependabot.
- `jarvis doctor` reports whether `~/.jarvis` is readable by anyone else, which service
  manager supervises the process (and what is unavailable when nothing does), and what is
  wrong with a malformed `JARVIS_PIN`.
- The README documents the approval bridge, what is stored on disk and what is sent to
  which third party, and the single-owner assumptions the deployment rests on.
- Test coverage is measured (94%) and CI fails below that floor.
- Retention: `TRANSCRIPT_RETENTION_DAYS` and `TASK_RETENTION_DAYS` (both off by default)
  prune at the top of `jarvis serve`, and `jarvis forget` does it on demand. A finished
  task you have not been told about is never deleted.

### Changed

- **Calls say a thing once.** Reviewing a week of transcripts turned up the same shape
  everywhere: "let me set that up for you" followed by "all set", four spoken turns for
  one PIN, a call-back that delivered its greeting twice. The voice prompt now forbids
  announcing an action and then confirming it, names the tools too fast to be worth
  announcing at all, and stops the model predicting the PIN or claiming to know what a
  running task will come back with. `mark_reported` and `end_session` are registered
  `silent=True`: their results no longer buy a spoken turn, which is what made a call-back
  repeat its own greeting.

- **The repository is now `jarvis-voice-agent`** (was `garmin-voice-agent`; GitHub
  redirects the old URL). The package, the CLI and `~/.jarvis` are unchanged.
- **`JARVIS_PIN` must be 6 to 8 digits.** `jarvis serve` refuses to start on anything else.
  An existing install with a shorter PIN must change it before deploying this.
- `~/.jarvis` and its subdirectories are created mode 0700, and transcripts, `tasks.db`,
  task logs and reports mode 0600. An existing tree is tightened in place on the next start.
- Every dependency now carries a version range instead of being unbounded.

### Fixed

- Tests could read a developer's real `.env`, which once printed a live admin key into
  pytest output, and assumed the host they grew up on (systemd on `PATH`, no forced
  colour, a short temp directory). The suite now passes on Linux and macOS alike.
- `/openapi.json` was served through the public tunnel.
- Caller phone numbers were written to the log in full.
- The README listed 13 of the voice model's 19 tools, and the docs still described task
  kinds (`coding`, `cowork`) that were removed in August 2026.
