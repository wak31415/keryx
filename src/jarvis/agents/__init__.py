"""The coding agents Jarvis can hand work to, one module per backend.

`base` is what every backend shares — the `AgentRunner` protocol, `RunResult`, the
`SPOKEN_SUMMARY:` / `RESTART_REQUIRED:` text protocol and the scripted fake. Each other
module is one agent: `claude` drives the Claude Agent SDK.
"""
