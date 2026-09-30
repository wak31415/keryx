"""Restarting the service across the death of the process that asks for it.

Six modules, three of them halves of one flow: `coordinator` asks and phones back,
`service` talks to systemd/launchd, `store` is the `restart.json` handover between the
process that asks and the one that comes back, `version` is what is *running* as opposed
to what is on disk, `watchdog` is the out-of-process alarm for a service that never came
back, and `logscan` reads the service's own log files by byte offset so a confirmation can
say what broke.

Nothing is re-exported here; import from the modules themselves. A package whose members
must be importable independently re-exports nothing, and this one's independence is
load-bearing: `logscan`, `store` and `service` are leaves that `watchdog` needs when the
application it is watching will not import.
"""
