"""The Issue reports section of `keryx setup`: whether Keryx may file issues about itself.

On, a bug or a feature request the owner says on a call is dispatched like any other work,
and the subagent files it on `ISSUE_REPO` with `gh` (`keryx.issues`). It publishes, so it
is off until the owner says yes here, and the wizard counts the section as left until it
has been walked once, whatever `doctor` says. Turned on, the section checks `gh`, offers to
sign it in on this terminal, and looks again. The repository is asked only on a review:
almost everyone files upstream, and `ISSUE_REPO` is one `keryx config set` away.
"""

from keryx.config.settings import REPO_PATTERN
from keryx.issues import GH_INSTALL_URL, GH_LOGIN
from keryx.setup.context import SetupContext

SECTION = "issues"


def _repo_problem(value: str) -> str | None:
    return None if REPO_PATTERN.fullmatch(value.strip()) else "As owner/name."


def run_section(ctx: SetupContext) -> None:
    ui, settings = ctx.ui, ctx.settings
    ui.note(
        "When you tell Keryx something about it is broken, or something it should be able "
        "to do, a subagent can take a short look and file it as a GitHub issue with `gh`. "
        "The look is kept short so you are not paying for Keryx's bugs, and nothing of "
        "yours goes in the issue — no numbers, names, or anything said on a call."
    )
    first = SECTION not in ctx.store.walked_sections()
    on = ui.confirm(
        "Let Keryx file bug reports and feature requests about itself?",
        default=True if first else settings.issue_reporting,
    )
    if not on:
        ctx.save({"ISSUE_REPORTING": False})
        ui.note("Off. A report you ask for anyway is written into the task's report instead.")
        return
    values: dict[str, object] = {"ISSUE_REPORTING": True}
    if ctx.review:
        repo = ui.text("Which GitHub repository should they go to?",
                       default=settings.issue_repo, validate=_repo_problem).strip()
        if repo != settings.issue_repo:
            values["ISSUE_REPO"] = repo
    if not ctx.save(values):
        return
    ui.note(f"They are filed on {ctx.settings.issue_repo}.")
    _check_gh(ctx)


def _check_gh(ctx: SetupContext) -> None:
    """`gh` installed and signed in, or the one thing that would make it so."""
    ui = ctx.ui
    with ui.spinner("Checking gh…"):
        status = ctx.probes.gh_status()
    if not status.installed:
        ui.warn(f"gh is not installed ({GH_INSTALL_URL}). Until it is, a report is written "
                "up but not filed; `keryx doctor` says so.")
        return
    if not status.signed_in:
        if not ui.confirm("gh is not signed in to GitHub. Sign in now?", default=True):
            ui.note("`gh auth login` when you are ready; `keryx doctor` says so until then.")
            return
        ctx.probes.run_login(list(GH_LOGIN))
        with ui.spinner("Checking gh…"):
            status = ctx.probes.gh_status()
        if not status.signed_in:
            ui.warn("gh is still not signed in; `gh auth login` to try again.")
            return
    ui.success("gh is signed in" + (f" as {status.account}" if status.account else ""))
    if status.keyring:
        ui.warn("gh keeps its token in the keyring, which the background service may not "
                "be able to open. `gh auth login --insecure-storage` keeps it in a private "
                "file instead, the way Keryx keeps its own.")
