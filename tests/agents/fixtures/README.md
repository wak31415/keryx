# Codex notification fixtures

What `codex app-server` sent through the `openai-codex` SDK (0.157.1, its bundled CLI) for
real turns recorded on 2026-09-26: one JSON object per notification, `{"method", "params"}`,
where `params` is the SDK's typed payload dumped with `model_dump(by_alias=True,
mode="json")`. The tests rebuild each line into that same typed model
(`NOTIFICATION_MODELS[method].model_validate(params)`), so the adapter is read against the
shapes it will meet, and a newer SDK that changes one fails here first.

The content is synthetic — thread, turn and item ids, paths, ray and request ids were
replaced, and the prompts were written for the recording — and a few lines were dropped to
keep them short: the owner's `hook/*` events, all but two `item/agentMessage/delta`s, all
but one `turn/diff/updated`, and all but two retrying `error`s. Every field name and nesting
is as recorded.

- `codex_app_run.jsonl` — a turn that runs a command, writes a file, calls a tool on a
  stand-in MCP server, and ends with `RESTART_REQUIRED:` and `SPOKEN_SUMMARY:`.
- `codex_app_failed.jsonl` — a refused credential: retrying `error`s (`willRetry: true`,
  `Reconnecting... n/5`), the terminal `error`, and `turn/completed` with status `failed`
  and the provider's 401 trailing its URL, ray and request id.
- `codex_app_interrupted.jsonl` — `turn/interrupt` mid-command: the command never
  completes, and `turn/completed` says `interrupted` with no error.
- `codex_app_steered.jsonl` — `turn/steer` mid-command: a second `userMessage` in the same
  turn, and a final answer that honours it.
- `codex_app_budget.jsonl` — **not recorded**: the failed turn with the terminal error's
  `codexErrorInfo` set to `sessionBudgetExceeded`, which cannot be provoked on demand.

Recorded against the same runtime: a `turn/steer` with no turn running (before one, or after
`turn/completed`) is `InvalidRequestError` (-32600, `no active turn to steer`), and
resuming a thread id the runtime does not know is `InvalidRequestError` (`no rollout found
for thread id …`).
