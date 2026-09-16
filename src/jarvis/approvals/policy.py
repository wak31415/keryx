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
import string
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jarvis.approvals.models import Kind, input_digest

log = logging.getLogger("jarvis.approvals.policy")

#: How much of a request may be said out loud in one go. An approval that does not fit is
#: not shortened, it is refused: what he hears has to be the whole of what runs.
MAX_SUMMARY_CHARS = 180
#: How long a question's option label may be. The label is the answer Claude is sent, so
#: one that does not fit is refused rather than cut.
MAX_OPTION_CHARS = 40
#: How many options a question may have and still be answerable on a keypad (1-9).
MAX_OPTIONS = 9

#: Tools that carry a question rather than a permission request. Answering them is a
#: `deny` carrying the answer, never an `allow` (models.Verdict).
QUESTION_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
#: Tools that write to a file, eligible only when the file is inside a known project.
EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
#: Path components (compared lowercased, for case-insensitive filesystems) that no phone
#: approval may write into, because what lands there is *executed* rather than read.
#: `.git` holds the hooks and the config (`core.fsmonitor`, `core.hooksPath`, a remote's
#: URL) that the allowlisted `git commit` and `git push` run or obey, and in a worktree it
#: is a file that says where all of that lives. `.claude` and `.mcp.json` hold the hooks,
#: permissions and servers the Claude CLI runs by itself on the next tool call or session.
EXECUTED_NAMES = frozenset({".git", ".claude", ".mcp.json"})
#: The fields a `Bash` request may carry and still be escalated. Anything else may change
#: how the command runs in a way the read-back never says, so an unknown field is refused;
#: `dangerouslyDisableSandbox` is allowed only when it is not set.
BASH_FIELDS = frozenset(
    {"command", "description", "timeout", "run_in_background", "dangerouslyDisableSandbox"}
)

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
#: Shell metacharacters. An allowlist means nothing if the command can chain, so a command
#: carrying any of these — quoted or not — is refused outright rather than parsed.
SHELL_METACHARACTERS = ("&", "|", ";", "`", "$(", ">", "<", "\n", "\r")
#: What a word may carry outside quotes and still mean exactly itself to bash and zsh:
#: nothing that expands (`$ ~ * ? [ { !`), quotes or escapes (`' " \`), or ends a command.
_BARE = frozenset(string.ascii_letters + string.digits + "_@%+=:,./-")
#: What double quotes do not stop the shell expanding.
_EXPANDS_IN_DOUBLE_QUOTES = frozenset("$`\\!")

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
    if "truncated" not in event:
        log.warning(
            "not escalating a %s prompt: the approval hook that sent it does not say whether "
            "it trimmed the request — re-run scripts/install-claude-hook.sh",
            tool or "?",
        )
        return None
    if event["truncated"] is not False:
        # The CLI runs the original, not the part of it the hook sent: nothing to decide on.
        log.info("not escalating a %s prompt: the hook had to trim it", tool or "?")
        return None

    try:
        _refuse_denied(tool, tool_input)
        kind, summary, options = _describe(tool, tool_input, event, settings)
        summary = _read_back(kind, summary)
    except Ineligible as reason:
        log.info("not escalating a %s prompt: %s", tool or "?", reason)
        return None

    return {
        "kind": kind,
        "summary": summary,
        "options": options,
        "input_sha": input_digest(tool_input),
    }


def _read_back(kind: Kind, summary: str) -> str:
    """The sentence he hears, or `Ineligible` when an approval would have to be cut.

    A question may be shortened: answering one runs nothing, and the answer is a label he
    picked. An approval may not. A cut read-back is a command whose tail runs unheard, and
    a long command is exactly where something gets hidden; nor does a keypad "yes" to a
    sentence nobody can hold in their head mean anything. So it is whole or not at all.
    """
    flat = _WHITESPACE.sub(" ", summary).strip()
    if kind is Kind.APPROVAL and len(flat) > MAX_SUMMARY_CHARS:
        raise Ineligible(f"the read-back is {len(flat)} characters and cannot be said whole")
    return _shorten(flat, MAX_SUMMARY_CHARS)


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
        _WHITESPACE.sub(" ", str(option.get("label"))).strip()
        for option in first.get("options") or []
        if isinstance(option, dict) and option.get("label")
    ]
    if not 1 <= len(labels) <= MAX_OPTIONS:
        raise Ineligible(f"it has {len(labels)} options, which does not fit a keypad")
    if any(len(label) > MAX_OPTION_CHARS for label in labels):
        raise Ineligible("an option is too long to read out as the answer it would send")
    return Kind.QUESTION, f"Claude is asking: {text}", labels


def _describe_edit(tool: str, tool_input: dict, settings) -> tuple[Kind, str, list[str]]:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not raw:
        raise Ineligible("there is no file path in the request")
    path = Path(str(raw))
    root = _inside(path, approval_roots(settings))
    if root is None:
        raise Ineligible("the file is outside every project root")
    target = path.expanduser().resolve()
    relative = target.relative_to(root)
    _refuse_executed(relative)
    verb = "create" if tool == "Write" else "edit"
    where = _project_name(target, root)
    # Where in the project, not just the name: the resolved path, so a symlink is read out
    # as the file it actually writes.
    inside = Path(*relative.parts[1:]).as_posix() if len(relative.parts) > 1 else target.name
    summary = f"Claude wants to {verb} the file {inside}, in {where}"
    return Kind.APPROVAL, summary, list(APPROVAL_OPTIONS)


def _refuse_executed(relative: Path) -> None:
    """Raise `Ineligible` if a path inside a project passes through an `EXECUTED_NAMES` entry.

    One exception: `.claude/worktrees/<name>/…` is a checkout Claude Code made, as ordinary
    as the one around it, so the walk carries on into it and judges its own `.git` and
    `.claude` in turn.
    """
    parts = [part.lower() for part in relative.parts]
    for index, part in enumerate(parts):
        if part not in EXECUTED_NAMES:
            continue
        if part == ".claude" and parts[index + 1 : index + 2] == ["worktrees"]:
            if len(parts) > index + 3:
                continue
        raise Ineligible(f"it writes into {relative.parts[index]}, which git or the CLI acts on")


def _describe_bash(tool_input: dict, event: dict, settings) -> tuple[Kind, str, list[str]]:
    unknown = set(tool_input) - BASH_FIELDS
    if unknown:
        raise Ineligible(f"it carries {', '.join(sorted(unknown))} as well as a command")
    if tool_input.get("dangerouslyDisableSandbox"):
        raise Ineligible("it asks to run outside the sandbox, which the read-back does not say")
    raw = str(tool_input.get("command") or "")
    # Both checks run on the command exactly as the shell will get it. Normalising first is
    # how a newline once became a space: eligible, read out as one line, and run as two.
    _refuse_unprintable(raw)
    for character in SHELL_METACHARACTERS:
        if character in raw:
            raise Ineligible("the command chains or redirects, so a prefix means nothing")
    argv = shell_words(raw)
    if not argv:
        raise Ineligible("there is no command in the request")
    _refuse_unlisted(argv, settings)
    command = _WHITESPACE.sub(" ", raw).strip()
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


def shell_words(command: str) -> list[str]:
    """The argv a shell will build from `command`, or `Ineligible` if that is in any doubt.

    A deliberately small subset of shell syntax that bash and zsh agree on: words split on
    plain spaces, made of characters that expand to nothing, `'single-quoted'` text, and
    `"double-quoted"` text with nothing in it the shell still expands. A variable, a glob, a
    tilde, a brace, an escape or a comment is refused rather than interpreted, because a
    guess about what the shell will make of it is a guess about what runs.
    """
    if "''" in command:
        # POSIX reads '' inside a word as two strings run together; zsh's RC_QUOTES reads
        # it as one literal quote. Two different argvs, so neither is assumed.
        raise Ineligible("it has '' in it, which bash and zsh read differently")
    words: list[str] = []
    word: list[str] | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if character == " ":
            if word is not None:
                words.append("".join(word))
                word = None
            index += 1
            continue
        if character in "'\"":
            end = command.find(character, index + 1)
            if end < 0:
                raise Ineligible("it has an unterminated quote")
            quoted = command[index + 1 : end]
            if character == '"' and any(item in _EXPANDS_IN_DOUBLE_QUOTES for item in quoted):
                raise Ineligible("a double-quoted word holds something the shell expands")
            word = [*(word or []), quoted]
            index = end + 1
            continue
        if character not in _BARE or (character == "=" and not word):
            # A leading `=` is zsh's path expansion (`=ls` is `/bin/ls`).
            raise Ineligible(f"{character!r} means something to the shell")
        word = [*(word or []), character]
        index += 1
    if word is not None:
        words.append("".join(word))
    return words


def _refuse_unlisted(argv: list[str], settings) -> None:
    """Raise `Ineligible` unless an `APPROVAL_BASH_ALLOW` entry allows exactly `argv`.

    An entry matches a command that is word for word the same. Only an entry with a rule in
    `ARGUMENTS` may be followed by anything, and then only by what that rule accepts: a
    prefix on its own says nothing about `--mirror`, `-F /etc/passwd` or `-p evilmodule`.
    """
    reason = "the command is not on the shell allowlist"
    for entry in settings.approval_bash_allow:
        try:
            allowed = shell_words(entry)
        except Ineligible:
            log.warning("ignoring APPROVAL_BASH_ALLOW entry %r: it is not a plain command", entry)
            continue
        if not allowed or argv[: len(allowed)] != allowed:
            continue
        if argv == allowed:
            return
        rule = ARGUMENTS.get(tuple(allowed))
        if rule is None:
            reason = f"only {entry!r} itself is allowed, with nothing after it"
            continue
        try:
            rule.check(argv[len(allowed) :])
        except Ineligible as refused:
            reason = str(refused)
            continue
        return
    raise Ineligible(reason)


@dataclass(frozen=True)
class Arguments:
    """What may follow an allowlisted command: named options and a check on the rest.

    Options are matched by their exact spelling. git reads any unambiguous abbreviation of a
    long option as the whole of it (`--mirr` is `--mirror`), so a set of full names is the
    only kind of list an abbreviation cannot slip past. Options may appear anywhere before a
    `--`, as git's own parser allows, and short ones may be clustered (`-am wip`).
    """

    #: Options that take no value.
    flags: frozenset[str]
    #: Options that take exactly one (`-m wip`, `-mwip`, `--message=wip`, `--message wip`).
    valued: frozenset[str]
    #: Raises `Ineligible` for positional arguments this command may not be given.
    positionals: Callable[[list[str]], None]

    def check(self, words: list[str]) -> None:
        positionals: list[str] = []
        index = 0
        while index < len(words):
            word = words[index]
            index += 1
            if word == "--":
                positionals.extend(words[index:])
                break
            if word == "-" or not word.startswith("-"):
                positionals.append(word)
            elif word.startswith("--"):
                name, has_value, _ = word.partition("=")
                if name in self.valued:
                    if not has_value:
                        index = _take_value(words, index, name)
                elif name not in self.flags or has_value:
                    raise Ineligible(f"{name} is not an option a keypad may approve")
            else:
                for position, letter in enumerate(word[1:], start=2):
                    option = f"-{letter}"
                    if option in self.valued:
                        if position == len(word):
                            index = _take_value(words, index, option)
                        break
                    if option not in self.flags:
                        raise Ineligible(f"{option} is not an option a keypad may approve")
        self.positionals(positionals)


def _take_value(words: list[str], index: int, option: str) -> int:
    if index >= len(words):
        raise Ineligible(f"{option} is missing its value")
    return index + 1


def _any_pathspecs(words: list[str]) -> None:
    """`git commit` pathspecs only choose which changes go in, and never leave the repo."""


def _push_destination(words: list[str]) -> None:
    """A remote *name* and plain branch or tag names: somewhere already configured, and
    nothing that force-pushes (`+`), deletes (`:main`), renames (`HEAD:main`) or globs."""
    if not words:
        return
    remote, *refspecs = words
    if not _REMOTE_NAME.fullmatch(remote):
        raise Ineligible(f"{remote!r} is not the name of a remote")
    for refspec in refspecs:
        if not _REF_NAME.fullmatch(refspec):
            raise Ineligible(f"{refspec!r} is not a plain branch or tag name")


#: A remote's name: no `/` (a path), no `:` (a URL or `host:path`), no leading `.` or `-`.
#: A name that is not a configured remote is read by git as a directory beside the
#: repository, which keeps the push on this machine.
_REMOTE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
#: A branch or tag name, spelled out: no `+`, `:`, `*`, `^`, `~` or `@{`.
_REF_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")

#: The allowlisted commands that may be given arguments at all, and which. Any other entry
#: in `APPROVAL_BASH_ALLOW` matches only itself, word for word.
ARGUMENTS: dict[tuple[str, ...], Arguments] = {
    # No -F/--file or -t/--template (a file from anywhere, into a commit `git push` sends
    # away), no -n/--no-verify (the hooks that check a commit), no --amend, -C/-c, --author,
    # --pathspec-from-file or anything else nobody needs to approve from a phone.
    ("git", "commit"): Arguments(
        flags=frozenset({"-a", "--all", "-q", "--quiet"}),
        valued=frozenset({"-m", "--message"}),
        positionals=_any_pathspecs,
    ),
    # No --force*, -f, --delete/-d, --mirror, --all/--branches, --tags, --prune, --repo,
    # --receive-pack/--exec, -o/--push-option or --no-verify; the destination is checked too.
    ("git", "push"): Arguments(
        flags=frozenset(
            {"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "-n", "--dry-run"}
        ),
        valued=frozenset(),
        positionals=_push_destination,
    ),
}
