"""Clients for the outside services a voice tool speaks to, one module per service.

Modules of the same shape: a `Protocol` naming what Jarvis needs, one implementation that
talks to exactly one third party over HTTP or ssh, and sometimes a `build_*` factory that
takes explicit values. `billing` reads the providers' own books, `cluster` reads Slurm over
the owner's own ssh ControlMaster, `gmail` answers a question about their mail, `slack`
posts a message, and `web_search` asks the Responses API. Each sits behind exactly one
voice tool: `web_search` behind a built-in (`tools/builtin_comms.py`), the rest behind a
plugin (`jarvis.plugins`), which is where the tool is built; this package is where the
client lives.

They share no code with each other — this is an address, not a module cluster. It is
`integrations/` and not `services/` because `service` already means "the systemd unit" in
this codebase (`SERVICE_MANAGER`, `restart/service.py`).

Nothing is re-exported here; import from the modules themselves. A package whose members
must be importable independently re-exports nothing, and no caller of one of these wants
the others' `urllib` handles or settings on the back of it.
"""
