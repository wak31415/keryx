"""`keryx setup`, `keryx auth` and the pieces they share.

The human path is `wizard.run_wizard`: sections in order, walked only where something is
missing. The agent path is the command line itself — `keryx config`, `keryx auth`,
`keryx memory seed`, `keryx doctor --json` — and `agent_instructions.md` is what
`keryx setup --agent-instructions` prints to say so.
"""
