"""`jarvis setup`, `jarvis auth` and the pieces they share.

The human path is `wizard.run_wizard`: sections in order, walked only where something is
missing. The agent path is the command line itself — `jarvis config`, `jarvis auth`,
`jarvis memory seed`, `jarvis doctor --json` — and `agent_instructions.md` is what
`jarvis setup --agent-instructions` prints to say so.
"""
