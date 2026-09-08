"""The approval bridge: a Claude Code prompt nobody answered becomes a phone call.

Nothing is re-exported here; import from the modules themselves. Importing any
submodule executes this file, so a package whose members must be importable
independently re-exports nothing: `models` is a dataclass module and must not
drag in the socket broker, the session and the transports behind it.
"""
