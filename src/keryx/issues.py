"""Reporting a bug in Keryx, or a feature it lacks, as an issue on Keryx's own repository.

Nothing here files anything. A subagent does, with `gh`, following the
`skills/keryx-report-issue` skill; this is only what that subagent is told — which
repository, where the code is, where the logs and the call it came from are — resolved
once from the settings, for whichever agent runs the task. There is no voice tool and no
task kind for it: the owner says what is wrong or missing, the voice model dispatches it
like any other work, and the subagent recognises it from its instructions.

It is off until the owner turns it on (`ISSUE_REPORTING`, which `keryx setup` asks
about), because it publishes: an issue is public. `gh_status` is the one look at whether
`gh` could file one, for `keryx doctor` and the wizard.

The skill is read from the checkout rather than from beside the running code, so an
install from a wheel with `KERYX_CHECKOUT` pointed at a clone still has it.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from keryx.continuity.transcripts import CALLS_DIR
from keryx.restart.logscan import log_dir
from keryx.skills import SKILL_FILE

if TYPE_CHECKING:  # pragma: no cover - typing only
    from keryx.config import Settings

#: The skill, relative to the checkout: it travels with the code it describes.
SKILL = Path("skills") / "keryx-report-issue" / SKILL_FILE
GITHUB_HOST = "github.com"
#: How to sign `gh` in, on the owner's terminal: its own prompts pick browser or token.
GH_LOGIN = ("gh", "auth", "login", "--hostname", GITHUB_HOST)
GH_INSTALL_URL = "https://cli.github.com"
#: `gh auth status` asks GitHub whether the token still works; a network that hangs is
#: not a reason for `keryx doctor` to.
GH_TIMEOUT_S = 15.0
#: "Logged in to github.com account NAME (…)", or "… as NAME (…)" before gh 2.40.
_ACCOUNT = re.compile(r"Logged in to \S+ (?:account|as) (\S+)")


@dataclass(frozen=True)
class GhStatus:
    """Whether `gh` could file an issue from this machine, and as whom."""

    installed: bool
    signed_in: bool = False
    account: str | None = None
    #: The token is in the system keyring, which a background service may not reach.
    keyring: bool = False


def gh_status() -> GhStatus:
    """What `gh auth status` says about github.com. Never raises; prints nothing."""
    gh = shutil.which("gh")
    if gh is None:
        return GhStatus(installed=False)
    try:
        done = subprocess.run(
            [gh, "auth", "status", "--hostname", GITHUB_HOST],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return GhStatus(installed=True)
    said = f"{done.stdout}\n{done.stderr}"
    account = _ACCOUNT.search(said)
    return GhStatus(
        installed=True,
        signed_in=done.returncode == 0,
        account=account.group(1) if account else None,
        keyring="(keyring)" in said,
    )


@dataclass(frozen=True)
class IssueReporting:
    """Where a report about Keryx goes, and what its subagent may read."""

    repo: str
    checkout: Path
    logs: Path
    calls: Path

    @property
    def skill(self) -> Path:
        return self.checkout / SKILL

    @classmethod
    def from_settings(cls, settings: "Settings") -> "IssueReporting | None":
        """None while it is off, or with no checkout that has the skill in it."""
        checkout = settings.checkout
        if not settings.issue_reporting or checkout is None or not (checkout / SKILL).is_file():
            return None
        return cls(
            repo=settings.issue_repo,
            checkout=checkout,
            logs=log_dir(settings.state_dir),
            calls=settings.data_dir / CALLS_DIR,
        )

    def transcript(self, session_id: str | None) -> Path | None:
        """The call a task was dispatched from, when it has a transcript on disk."""
        if not session_id:
            return None
        path = self.calls / f"{session_id}.log"
        return path if path.is_file() else None
