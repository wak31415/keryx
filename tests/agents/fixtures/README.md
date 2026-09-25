# Codex event fixtures

`codex exec --json` output, in the shapes Codex CLI 0.156 actually printed for real runs
recorded on 2026-09-24. The content is synthetic — ids, paths, messages and keys are made
up — but every event type, field name and nesting is copied from a real run, the way the
cluster fixtures were made:

- `codex_run.jsonl` — a turn that runs a command, writes a file, calls an MCP tool and
  finishes with `RESTART_REQUIRED:` and `SPOKEN_SUMMARY:`.
- `codex_failed.jsonl` — a model the plan does not offer: a non-fatal `error` item, then
  `turn.failed` with the provider's JSON body quoted as a string. Exit code 1.
- `codex_bad_key.jsonl` — a refused `CODEX_API_KEY`, which Codex quotes back masked.
  Exit code 1.
- `codex_interrupted.jsonl` — SIGINT to the process group mid-command: stdout simply stops,
  no `turn.failed`, exit code 1 within a fraction of a second.

A resume (`codex exec resume <id> --json -`) prints the same `thread.started` id it was
given, then an ordinary turn. A resume of an id Codex does not know prints nothing on
stdout and exits 1 with `Error: thread/resume: … no rollout found for thread id …` on
stderr.
