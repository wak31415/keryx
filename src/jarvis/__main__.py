"""`python -m jarvis` — how the restart watchdog is started.

`systemd-run` is handed a fixed argv and no shell, so the watchdog is started as
`sys.executable -m jarvis restart-watch`: the interpreter of the venv this process is
already running in, with no dependence on PATH or on where the `jarvis` console script
happened to be installed. Every other entry point goes through that console script.
"""

from jarvis.cli import app

if __name__ == "__main__":
    app()
