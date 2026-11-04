"""`python -m keryx` — the `keryx` command, run by this venv's own interpreter.

That is how a subagent is handed the command (`agents/base.py`): no dependence on PATH or
on where the console script happened to be installed. The restart watchdog is no longer
started this way, because this imports the whole application and the watchdog must start
when that will not (`python -m keryx.restart.watchdog`); a unit an older build armed still
runs `-m keryx restart-watch`, which is why that command remains.
"""

from keryx.cli import app

if __name__ == "__main__":
    app()
