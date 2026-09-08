"""What version is running, as opposed to what version is on disk.

Split out of `restart.py` (2026-09-02); the flow that uses it is documented there.

The distinction these five functions exist for is the one that makes a restart's
confirmation honest. A long-lived process keeps running the code it imported while the
checkout moves underneath it, so `git describe` at the moment a restart is *asked for*
answers "what would we load", not "what are we running" — and the normal order of events
(edit, commit, ask for the restart) puts the new commit on disk before the question is even
put, so a request-time read compares the new commit with itself and reports that nothing
loaded. `mark_running` stamps the version at process start, which is the one moment the
checkout and the running code are the same thing.

All of it is decoration in the sense that nothing here is allowed to fail a restart: no
`git`, no stamp, or an unwritable `data_dir` each cost one line of the spoken summary.
"""

import json
import logging
import subprocess
from pathlib import Path

from jarvis.restart.logscan import marks

log = logging.getLogger("jarvis.restart")

#: Where `jarvis serve` stamps the version it imported, under `data_dir`. See `mark_running`.
RUNNING_NAME = "running-version"
#: Where `jarvis serve` stamps how far the logs had got when it started. See `mark_startup_logs`.
STARTUP_MARKS_NAME = "startup-log-marks.json"
#: Bound on `git describe`, which is only ever decoration on the summary.
GIT_TIMEOUT_S = 2.0


def current_version(repo: Path | None = None) -> str | None:
    """`git describe` of the checkout we are running from — decoration, never required."""
    root = repo or Path(__file__).resolve().parents[2]
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "describe", "--always", "--dirty", "--abbrev=7"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def mark_running(data_dir: Path, repo: Path | None = None) -> str | None:
    """Stamp the version this process imported. Called once, at the top of `jarvis serve`.

    The checkout keeps moving underneath a long-lived process. Reading it when a restart is
    *asked for* therefore answers "what will we load", not "what are we running" — and the
    normal flow (edit, commit, ask for the restart) puts the new commit on disk before the
    question is ever put, so the two reads match and the restart looks like it loaded
    nothing. Process start is the one moment the checkout and the running code are the same
    thing, so it is the only honest place to take the "before".
    """
    version = current_version(repo)
    path = data_dir / RUNNING_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{version}\n" if version else "")
    except OSError:
        # Decoration, like `current_version` itself: a missing stamp costs one comparison,
        # and falling back to the checkout is no worse than what there was before.
        log.warning("could not stamp the running version at %s", path)
    return version


def running_version(data_dir: Path) -> str | None:
    """The version the live process stamped at startup, or None if it never got the chance."""
    try:
        return (data_dir / RUNNING_NAME).read_text().strip() or None
    except OSError:
        return None


def loaded_version(data_dir: Path, repo: Path | None = None) -> str | None:
    """What the service process is running: its own stamp, else the checkout as a guess.

    The guess is what a service too old to stamp anything falls back to, and it is wrong in
    exactly the way described in `mark_running` — but a wrong guess and no answer read the
    same over the phone, and the guess is at least right when nothing has been committed.
    """
    return running_version(data_dir) or current_version(repo)


def mark_startup_logs(data_dir: Path) -> dict[str, int]:
    """Stamp how far the service logs had got when *this* process started.

    There are two questions about a restart and they want different starting points.

    The watchdog asks "did anything come back at all", so it has to read from the moment
    the restart was *requested* — there may be no new process to have marked anything.

    `resume()` asks the narrower and more useful question, "did I come up clean", and for
    that the request is the wrong mark: it includes the dying process's last gasps. A
    Python interpreter shutting down with a subagent subprocess still open reliably prints
    `RuntimeError: Event loop is closed` out of `base_subprocess.__del__`, which is noise
    from a process that is already gone — and read as this restart's error it put "but 1
    error in the log since" on the confirmation call for a restart that went perfectly.
    Every self-edit restart would have said it, which is the one case the check exists for.
    """
    found = marks(data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / STARTUP_MARKS_NAME).write_text(json.dumps(found), encoding="utf-8")
    except OSError:
        log.warning("could not stamp the startup log marks in %s", data_dir)
    return found


def startup_log_marks(data_dir: Path) -> dict[str, int] | None:
    """What this process stamped at startup, or None if it never got the chance."""
    try:
        found = json.loads((data_dir / STARTUP_MARKS_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return found if isinstance(found, dict) and found else None
