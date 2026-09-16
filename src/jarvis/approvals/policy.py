"""Which prompts may ever be escalated to a phone call, and what he is told about them.

This is the *primary* control on the whole bridge, not a second layer on top of one. A
`PermissionRequest` hook that returns `allow` appears to skip the CLI's own re-check of
`permissions.deny` (measured 2026-08-26; see the report in
`reports/jarvis-approval-bridge/`), so nothing behind this module is protecting him:
whatever `classify` calls eligible is what a keypad digit can run.

So it is an allowlist, it starts small, and the denylist wins over it. Everything not
explicitly named comes back ineligible, which means the hook says nothing and the prompt
waits on his screen exactly as it does today.

Nothing here reads a file or touches the network: it is pure, so the tests are the spec.
"""

import logging
import re
import unicodedata
from pathlib import Path

from jarvis.approvals.models import Kind, input_digest

log = logging.getLogger("jarvis.approvals.policy")

#: How much of a request may be said out loud in one go.
MAX_SUMMARY_CHARS = 180
#: How much of a question's option label survives into the keypad menu.
MAX_OPTION_CHARS = 40
#: How many options a question may have and still be answerable on a keypad (1-9).
MAX_OPTIONS = 9

#: Tools that carry a question rather than a permission request. Answering them is a
#: `deny` carrying the answer, never an `allow` (models.Verdict).
QUESTION_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
#: Tools that write to a file, eligible only when the file is inside a known project.
EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

#: The permission modes in which nobody is really being asked. `PermissionRequest` does
#: not fire under `claude -p` at all, so this is belt and braces for the interactive case.
SILENT_MODES = frozenset({"bypassPermissions"})

#: Substrings that make a request permanently ineligible, matched case-insensitively
#: against every value in the tool input. Deliberately blunt: a false positive costs him
#: nothing (the prompt waits on screen), a false negative costs him a key.
DENY_SUBSTRINGS = (
    ".env",
    ".pem",
    ".key",
    "id_rsa",
    "id_ed25519",
    "/secrets/",
    "/credentials/",
    "/.ssh/",
    "/.aws/",
    "/.gnupg/",
    "-----begin",
    "sk-ant-",
    "ghp_",
    "github_pat_",
    "akia",
    "authorization:",
    "bearer ",
)
#: The same, for a shell command specifically.
DENY_COMMAND_SUBSTRINGS = (
    "sudo",
    "rm -rf",
    "rm -fr",
    "mkfs",
    "dd if=",
    "chmod 777",
    "shutdown",
    "reboot",
    "systemctl",
    "launchctl",
    "git push --force",
    "git push -f",
    "gh pr merge",
    "gh release",
    "npm publish",
    "pip install",
    "uv publish",
    "curl",
    "wget",
    "ssh ",
    "scp ",
    "history",
    "printenv",
    "env |",
)
#: Shell metacharacters. A prefix allowlist means nothing if the command can chain, so a
#: command carrying any of these is refused outright rather than parsed.
SHELL_METACHARACTERS = ("&", "|", ";", "`", "$(", ">", "<", "\n", "\r")

#: The default `approve`/`reject` menu of an ordinary tool call. `0` (leave it) is added
#: by `ApprovalRequest.menu`, and it is always available.
APPROVAL_OPTIONS = ["approve", "reject"]

_WHITESPACE = re.compile(r"\s+")


class Ineligible(Exception):
    """Raised inside `classify` to say why a request may never be escalated."""


def _flatten(value: object) -> str:
    """Every string anywhere in a tool input, lowercased and run together.

    Used only for the denylist: it does not matter *which* field held `.ssh/`, only that
    something did.
    """
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, dict):
        return " ".join(_flatten(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(item) for item in value)
    return str(value).lower()


def _shorten(text: str, limit: int) -> str:
    flat = _WHITESPACE.sub(" ", str(text)).strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def approval_roots(settings) -> list[Path]:
    """The directories a file may be written in and still be eligible.

    `APPROVAL_ROOTS` when it is set, otherwise the projects root plus every explicitly
    configured project. Resolved, because `~/projects` is a symlink into `/data` on this
    machine and a prefix test against the wrong one of those silently allows nothing.
    """
    configured = [Path(item).expanduser() for item in settings.approval_roots]
    if not configured:
        explicit = (Path(item).expanduser() for item in settings.projects.values())
        configured = [settings.projects_root, *explicit]
    roots = []
    for root in configured:
        try:
            roots.append(root.resolve())
        except OSError:  # pragma: no cover - a root that cannot be resolved is simply not one
            log.warning("could not resolve the approval root %s", root)
    return roots


def _inside(path: Path, roots: list[Path]) -> Path | None:
    """The root `path` sits under, or None. Resolution is `strict=False`: the file a
    `Write` is about does not exist yet, and `..` still has to be collapsed before the
    prefix test or `project/../../.ssh/id_rsa` walks straight out."""
    try:
        target = path.expanduser().resolve()
    except OSError:  # pragma: no cover
        return None
    for root in roots:
        if target == root or root in target.parents:
            return root
    return None


def _project_name(path: Path, root: Path) -> str:
    """What to call the place this is happening, for a sentence he has to hear."""
    relative = path.relative_to(root) if path != root else Path()
    return relative.parts[0] if relative.parts else root.name


def classify(event: dict, settings) -> dict | None:
    """What to escalate about `event`, or None if it may never be escalated.

    `event` is the raw `PermissionRequest` hook payload. The return is the keyword
    arguments an `ApprovalRequest` is built from — `kind`, `summary`, `options`,
    `input_sha` — so the broker never has to look at `tool_input` itself.
    """
    if event.get("hook_event_name") != "PermissionRequest":
        return None
    if event.get("permission_mode") in SILENT_MODES:
        return None

    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    try:
        _refuse_denied(tool, tool_input)
        kind, summary, options = _describe(tool, tool_input, event, settings)
    except Ineligible as reason:
        log.info("not escalating a %s prompt: %s", tool or "?", reason)
        return None

    return {
        "kind": kind,
        "summary": _shorten(summary, MAX_SUMMARY_CHARS),
        "options": options,
        "input_sha": input_digest(tool_input),
    }


def _refuse_denied(tool: str, tool_input: dict) -> None:
    """Raise `Ineligible` if anything in the request is on the denylist."""
    haystack = _flatten(tool_input)
    for needle in DENY_SUBSTRINGS:
        if needle in haystack:
            raise Ineligible(f"it mentions {needle}")
    if tool != "Bash":
        return
    command = str(tool_input.get("command") or "").lower()
    for needle in DENY_COMMAND_SUBSTRINGS:
        if needle in command:
            raise Ineligible(f"the command contains {needle!r}")


def _describe(tool: str, tool_input: dict, event: dict, settings) -> tuple[Kind, str, list[str]]:
    """The kind, the spoken read-back and the keypad menu, or `Ineligible`."""
    if tool == "AskUserQuestion":
        return _describe_question(tool_input)
    if tool == "ExitPlanMode":
        plan = _shorten(tool_input.get("plan") or "", MAX_SUMMARY_CHARS)
        summary = f"Claude wants to start on a plan it has written: {plan}"
        return Kind.QUESTION, summary, ["go ahead"]
    if tool in EDIT_TOOLS:
        return _describe_edit(tool, tool_input, settings)
    if tool == "Bash":
        return _describe_bash(tool_input, event, settings)
    raise Ineligible(f"{tool or 'that tool'} is not on the escalation allowlist")


def _describe_question(tool_input: dict) -> tuple[Kind, str, list[str]]:
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not questions:
        raise Ineligible("the question payload had no questions in it")
    if len(questions) > 1:
        # More than one question in one prompt has no keypad shape; he answers on screen.
        raise Ineligible("it asks more than one question at once")
    first = questions[0]
    if not isinstance(first, dict):
        raise Ineligible("the question payload was not the shape we know")
    text = _shorten(first.get("question") or "", MAX_SUMMARY_CHARS)
    if not text:
        raise Ineligible("the question was empty")
    labels = [
        _shorten(option.get("label") or "", MAX_OPTION_CHARS)
        for option in first.get("options") or []
        if isinstance(option, dict) and option.get("label")
    ]
    if not 1 <= len(labels) <= MAX_OPTIONS:
        raise Ineligible(f"it has {len(labels)} options, which does not fit a keypad")
    return Kind.QUESTION, f"Claude is asking: {text}", labels


def _describe_edit(tool: str, tool_input: dict, settings) -> tuple[Kind, str, list[str]]:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        raise Ineligible("there is no file path in the request")
    path = Path(str(raw))
    root = _inside(path, approval_roots(settings))
    if root is None:
        raise Ineligible("the file is outside every project root")
    verb = "create" if tool == "Write" else "edit"
    where = _project_name(path.expanduser().resolve(), root)
    summary = f"Claude wants to {verb} the file {path.name}, in {where}"
    return Kind.APPROVAL, summary, list(APPROVAL_OPTIONS)


def _describe_bash(tool_input: dict, event: dict, settings) -> tuple[Kind, str, list[str]]:
    raw = str(tool_input.get("command") or "")
    # Both checks run on the command exactly as the shell will get it. Normalising first is
    # how a newline once became a space: eligible, read out as one line, and run as two.
    _refuse_unprintable(raw)
    for character in SHELL_METACHARACTERS:
        if character in raw:
            raise Ineligible("the command chains or redirects, so a prefix means nothing")
    command = _WHITESPACE.sub(" ", raw).strip()
    if not command:
        raise Ineligible("there is no command in the request")
    if not any(_matches_prefix(command, prefix) for prefix in settings.approval_bash_allow):
        raise Ineligible("the command is not on the shell allowlist")
    cwd = Path(str(event.get("cwd") or "."))
    root = _inside(cwd, approval_roots(settings))
    if root is None:
        raise Ineligible("it would run outside every project root")
    where = _project_name(cwd.expanduser().resolve(), root)
    return Kind.APPROVAL, f"Claude wants to run: {command}, in {where}", list(APPROVAL_OPTIONS)


def _refuse_unprintable(command: str) -> None:
    """Raise `Ineligible` for any character that is neither printable nor a plain space.

    Unicode categories `C*` (NUL and the other C0 and C1 controls, DEL, zero-width and
    bidirectional formatting) and `Z*` other than U+0020 (tabs are `Cc`; line and paragraph
    separators, no-break spaces). A shell may split on some of them and a read-back hides all
    of them, and a command that needs one is not a command to approve by ear.
    """
    for character in command:
        if character != " " and unicodedata.category(character)[0] in "CZ":
            raise Ineligible(f"the command carries U+{ord(character):04X}, which is not printable")


def _matches_prefix(command: str, prefix: str) -> bool:
    """True when `command` *is* `prefix` or starts with it followed by a word boundary.

    `git commit` must not match `git committer-is-not-a-thing`, and it must not match
    `git commitfoo`; only `git commit` and `git commit -m …`.
    """
    prefix = _WHITESPACE.sub(" ", prefix).strip()
    if not prefix:
        return False
    return command == prefix or command.startswith(prefix + " ")
