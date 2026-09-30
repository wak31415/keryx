"""The coding agents Keryx can hand work to, one module per backend.

`base` is what every backend shares — the `AgentRunner` protocol, `RunResult`, the
`SPOKEN_SUMMARY:` / `RESTART_REQUIRED:` text protocol and the scripted fake — and `session`
is the one session they all run through. Each other module is one agent: `claude` drives
the Claude Agent SDK, `codex` the `openai-codex` SDK.
"""
