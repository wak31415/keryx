"""Clients for the outside services a voice tool speaks to, one module per service.

Four modules of the same shape: a `Protocol` naming what Jarvis needs, one implementation
that talks to exactly one third party over HTTP or ssh, and sometimes a `build_*` factory
that picks one from settings. `billing` reads the providers' own books, `cluster` reads
Slurm through the cluster-compute skill's guard, `slack` posts a message, and
`web_search` asks the Responses API. Each sits behind exactly one of the voice model's
tools, whose *registration* is in `tools/builtin_<domain>.py`; this package is where the
thing being registered lives.

They share no code with each other — this is an address, not a module cluster. It is
`integrations/` and not `services/` because `service` already means "the systemd unit" in
this codebase (`SERVICE_MANAGER`, `restart/service.py`).

Nothing is re-exported here; import from the modules themselves. A package whose members
must be importable independently re-exports nothing, and no caller of one of these wants
the other three's `urllib` handles or settings on the back of it.
"""
