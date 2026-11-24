"""Plugins: the optional voice tools, installed into the owner's tools directory when wanted.

Some tools are not Keryx's to offer on every machine — `slack_report`, `slack_read`,
`slack_message`, `check_email`, `check_calendar`, `add_calendar_event`, `find_document`,
`check_billing` and `cluster_stats` each need something only some owners have — so none is
registered from the source tree. A plugin exists when the owner turns it on, and it is then
an ordinary custom tool (`keryx.tools.custom`): two files in `DATA_DIR/tools`,

- `<name>.py`, one line calling `keryx.plugins.<module>.<name>_tool`, so the logic stays
  here with its tests and a fix reaches every installed copy; and
- `<name>.toml`, the plugin's settings, commented, which the owner may edit by hand and the
  wizard edits in place.

Its secrets are not in that file: a token or an admin key stays in `secrets.toml` through
the store (`keryx config set KEY --stdin`), and the Google sign-in in its own token file.

`PLUGINS` is the table everything else reads, in the style of `agents.registry.BACKENDS`:
what each is called, what the wizard asks (`important`; the rest stay in the file, with
their comments) and which secrets it uses. `keryx plugins` and the Plugins section of
`keryx setup` are the two ways in.

**A renamed plugin's old files are read, and never moved unasked.** `slack_report` was
`send_to_slack` until 2026-11-23 (`Plugin.formerly`), and a machine that installed it then
has `send_to_slack.py` and `send_to_slack.toml`. Those keep working with nothing asked of
the owner: every path here goes by the name the files have on that machine (`files_name`),
so what reads the settings reads them, what writes them writes them where they are, and
the tool they define is offered under its new name. Only `keryx plugins install
slack_report`, typed by the owner, renames the pair in place (`adopt`): several builds may
run against one tools directory, and an older one finds its plugin only under the name it
knows. The old name is still taken on a command line, and is never given to a new file.

Every file is written through `config.files.write_private`, 0600, because the loader refuses
a file anyone else could write. A value is validated before it is written (bare words,
choices, numbers) and quoted by `toml_value`, so nothing written can end its own line.
"""

import contextlib
import contextvars
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from keryx.config.files import read_toml, toml_key, toml_value, write_private

if TYPE_CHECKING:  # pragma: no cover - typing only
    from keryx.config import Settings

#: What a word in a plugin's settings has to be: a cluster alias or partition, a channel id,
#: a model name. The first two reach a remote shell, so anything else is refused on write.
WORD = re.compile(r"[A-Za-z0-9_.-]+")

#: Where a turned-off plugin's settings wait, so turning it back on remembers them.
OFF_SUFFIX = ".toml.off"
#: The most `check_calendar` may be set to say at once: under one page of Google's, so it
#: can always tell that there were more. `integrations.calendar` holds the same number.
MAX_EVENTS_SETTING = 100

Kind = Literal["word", "choice", "choices", "number", "path", "line", "table", "tables"]
#: The kinds that are a TOML table rather than one line: never set with `--set KEY=VALUE`.
TABLE_KINDS = ("table", "tables")


class PluginConfigError(ValueError):
    """A plugin's settings that do not parse or do not validate, in a sentence to print."""


@dataclass(frozen=True)
class Key:
    """One setting in a plugin's TOML: its kind, its default, and how the wizard asks it."""

    name: str
    kind: Kind
    default: Any
    label: str
    choices: tuple[str, ...] = ()
    #: For a number: whether 0 is allowed (a budget of 0 is "none"; a timeout of 0 is not).
    positive: bool = False
    #: For a number: the most it may be, when there is a most.
    maximum: float | None = None
    #: For `tables` (named tables of the same settings): what each one holds, and what
    #: its table is called in the file before its name (`[workspace-work]`). Each is a
    #: table of its own at the top of the file, one level deep, because that is a shape a
    #: build of Keryx from before the key carries through a rewrite untouched.
    fields: tuple["Key", ...] = ()
    prefix: str = ""
    #: Written only by the plugin's sign-in (`Plugin.sign_in`), from what the service it
    #: connects to said: never taken from a command line, where it could say otherwise.
    by_sign_in: bool = False
    #: A setting this one replaced: its name, and what each value it could hold is as a
    #: value of this one (`was`). A file that still has it and not this one is read by
    #: it; it is never written, and never carried through a rewrite.
    formerly: str = ""
    was: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Plugin:
    """One plugin: its tool's name, what it is for, and what configures it."""

    name: str
    title: str
    summary: str
    #: Ticked on a first walk of the wizard: the ones most owners want.
    default: bool
    #: The keys the wizard asks; the rest stay in the file, with their comments.
    important: tuple[str, ...]
    #: The store keys of the secrets it uses, which live in `secrets.toml`.
    secrets: tuple[str, ...]
    keys: tuple[Key, ...]
    #: The `keryx.plugins` module that builds its tool.
    module: str
    #: What must hold before it can be installed, beyond each key being valid.
    problem: Callable[[dict[str, Any]], str | None] = lambda _values: None
    #: The Google service it reads or writes with the one sign-in (`integrations.google`'s
    #: name for it), for a plugin that has one: its "secret" is `keryx auth login google`.
    google: str = ""
    #: Whether it answers through the bundled `claude` CLI, and so needs the claude extra.
    claude: bool = False
    #: Ticked on a first walk as well, but only once its Google service is connected:
    #: wanted by most, and not worth a sign-in of its own to somebody who has none.
    default_when_connected: bool = False
    #: The name it had before it was renamed, whose files are still read (`files_name`).
    formerly: str = ""
    #: The command that connects it, which is the only writer of its `by_sign_in` keys.
    sign_in: str = ""
    #: The plugin whose sign-in turns this one on and off (Slack's two that act as the
    #: owner follow what is ticked there), so the wizard's list does not ask about it.
    follows: str = ""

    def key(self, name: str) -> Key:
        return next(key for key in self.keys if key.name == name)


def _slack_problem(values: dict[str, Any]) -> str | None:
    if values["channel_id"] or values["mcp_server"]:
        return None
    return ("channel_id is empty (`keryx auth login slack` finds it, or name an mcp_server "
            "whose config has one)")


def _cluster_problem(values: dict[str, Any]) -> str | None:
    return None if values["clusters"] else "[clusters] names no host"


#: What a Slack app may do beyond sending the owner reports and files, in the order it
#: is shown and written (`integrations.slack.Permission`): any set of them.
SLACK_ALLOW = ("requests", "read", "send")
#: What `allow` replaced on 2026-11-24: `access`, one of four ordered levels, each
#: holding the one below. A file that still says a level is read as the set it was.
SLACK_ACCESS = {
    "announce": [],
    "converse": ["requests"],
    "read": ["requests", "read"],
    "write": ["requests", "read", "send"],
}
#: What a Slack workspace is, beside its tokens: the first one at the top of the file, and
#: each further one in a `[workspace-NAME]` table of the same keys.
#: `allow`, `owner_id` and what Slack said of the workspace are settled by connecting it
#: (`keryx auth login slack`), which checks each token against them. One owner, fixed at
#: connect: `--set owner_id=…` would be an option to change it, so there is none.
_SLACK_WORKSPACE = (
    Key("channel_id", "word", "", "Where it posts: your member id, or a channel's id"),
    Key("allow", "choices", [], "What Keryx may do there beyond sending you reports",
        choices=SLACK_ALLOW, by_sign_in=True, formerly="access", was=SLACK_ACCESS),
    Key("owner_id", "word", "",
        "Your own member id: only direct messages from it are ever acted on", by_sign_in=True),
    Key("team", "line", "", "The workspace's name, as Slack gave it", by_sign_in=True),
    Key("team_id", "word", "", "The workspace's id, as Slack gave it", by_sign_in=True),
    Key("bot_user_id", "word", "",
        "The app's own member id there: what a mention is of, and never written to as you",
        by_sign_in=True),
)

PLUGINS: dict[str, Plugin] = {
    "slack_report": Plugin(
        name="slack_report",
        title="Slack",
        summary='"Send me that on Slack" posts to your DM; PIN lockouts are posted there too',
        formerly="send_to_slack",
        default=True,
        important=("channel_id",),
        secrets=("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_USER_TOKEN",
                 "SLACK_WORKSPACE_TOKENS"),
        keys=(
            *_SLACK_WORKSPACE,
            Key("mcp_server", "word", "",
                "The user-scope MCP server in ~/.claude.json that gives subagents Slack, "
                "used only while Keryx has no bot token of its own"),
            Key("workspaces", "tables", {}, "Each further workspace, by the name you gave it",
                fields=_SLACK_WORKSPACE, prefix="workspace-", by_sign_in=True),
        ),
        module="slack",
        problem=_slack_problem,
        sign_in="keryx auth login slack",
    ),
    "slack_read": Plugin(
        name="slack_read",
        title="Slack reading",
        summary='"Anything waiting for me on Slack?" read as you, on a call, when you ask',
        default=False,
        important=(),
        secrets=(),
        keys=(
            Key("model", "word", "claude-opus-5-5", "The model that answers a question"),
            Key("effort", "choice", "low", "How hard it thinks", choices=("low", "medium", "high")),
        ),
        module="slack_read",
        follows="slack_report",
    ),
    "slack_message": Plugin(
        name="slack_message",
        title="Slack messages as you",
        summary='"Tell Sam I am running late": one message sent as you, after you say yes',
        default=False,
        important=(),
        secrets=(),
        keys=(),
        module="slack_message",
        follows="slack_report",
    ),
    "check_email": Plugin(
        name="check_email",
        title="Email answers",
        summary="Questions about your email, answered on a call in about five seconds",
        default=True,
        important=(),
        secrets=(),
        keys=(
            Key("model", "word", "claude-opus-5-5", "The model that answers"),
            Key("effort", "choice", "low", "How hard it thinks", choices=("low", "medium", "high")),
        ),
        module="email",
        google="gmail",
        claude=True,
    ),
    "check_calendar": Plugin(
        name="check_calendar",
        title="Calendar answers",
        summary="What is on your calendar, said on a call in a second or two",
        default=False,
        important=(),
        secrets=(),
        keys=(
            Key("calendars", "choice", "selected", "Which calendars it reads",
                choices=("selected", "primary")),
            Key("max_events", "number", 25, "The most events one answer holds", positive=True,
                maximum=MAX_EVENTS_SETTING),
        ),
        module="calendar",
        google="calendar",
        default_when_connected=True,
    ),
    "add_calendar_event": Plugin(
        name="add_calendar_event",
        title="Add to calendar",
        summary='"Put the dentist on my calendar Thursday at three": one event, nobody invited',
        default=False,
        important=(),
        secrets=(),
        keys=(
            Key("default_minutes", "number", 60,
                "How long an event is when you gave no end, in minutes", positive=True),
        ),
        module="calendar",
        google="calendar",
    ),
    "find_document": Plugin(
        name="find_document",
        title="Document answers",
        summary="A file found in your Drive on a call, and a question about it answered",
        default=False,
        important=(),
        secrets=(),
        keys=(
            Key("model", "word", "claude-opus-5-5", "The model that answers"),
            Key("effort", "choice", "low", "How hard it thinks", choices=("low", "medium", "high")),
        ),
        module="documents",
        google="drive",
        claude=True,
        default_when_connected=True,
    ),
    "check_billing": Plugin(
        name="check_billing",
        title="Billing",
        summary='"What am I spending?" read from the provider\'s billing API',
        default=False,
        important=("provider", "monthly_budget"),
        secrets=("OPENAI_ADMIN_KEY", "ANTHROPIC_ADMIN_KEY"),
        keys=(
            Key("provider", "choice", "auto", "Whose bill it reports by default",
                choices=("auto", "openai", "anthropic")),
            Key("monthly_budget", "number", 0, "Your monthly budget in dollars (0 for none)"),
            Key("openai_project_id", "word", "", "Narrows OpenAI spend to one project"),
            Key("openai_api_key_id", "word", "", "Narrows OpenAI token usage to one key id"),
            Key("anthropic_workspace_id", "word", "", "Narrows Anthropic spend to one workspace"),
        ),
        module="billing",
    ),
    "cluster_stats": Plugin(
        name="cluster_stats",
        title="Cluster stats",
        summary='"What\'s free on the cluster?" read from Slurm over your open ssh login',
        default=False,
        important=("clusters",),
        secrets=(),
        keys=(
            Key("clusters", "table", {}, "The hosts it asks, each with its GPU partition"),
            Key("guard", "path", "", "A guard script of your own (blank: the built-in one)"),
            Key("timeout_s", "number", 20.0, "How long a cluster may take to answer",
                positive=True),
        ),
        module="cluster",
        problem=_cluster_problem,
    ),
}


def canonical(name: str) -> str:
    """`name` as the plugin is called now: a name it had before a rename is still taken."""
    return next((spec.name for spec in PLUGINS.values() if spec.formerly == name), name)


def plugin(name: str) -> Plugin:
    """The plugin called `name`, or a `PluginConfigError` naming the ones there are."""
    try:
        return PLUGINS[canonical(name)]
    except KeyError:
        raise PluginConfigError(
            f"there is no plugin called {name!r} ({', '.join(PLUGINS)})"
        ) from None


# --- where the files are -----------------------------------------------------------------


#: What a plugin's files end in: its tool, its settings, and the settings set aside while
#: it is off. (A draft of the tool is the fourth: `_<name>.py`.)
_SUFFIXES = (".py", ".toml", OFF_SUFFIX)


def _has_files(settings: "Settings", stem: str) -> bool:
    tools = settings.custom_tools_dir
    return any((tools / f"{stem}{suffix}").exists() for suffix in _SUFFIXES) or (
        tools / f"_{stem}.py").exists()


def files_name(settings: "Settings", name: str) -> str:
    """The name the plugin's files go by on this machine: its own, or the one it had
    before a rename while those files are what is there. Never both: once a file of the
    new name exists, that is the name."""
    spec = PLUGINS.get(canonical(name))
    if spec is None:
        return name
    if spec.formerly and not _has_files(settings, spec.name) and _has_files(
            settings, spec.formerly):
        return spec.formerly
    return spec.name


def tool_path(settings: "Settings", name: str) -> Path:
    return settings.custom_tools_dir / f"{files_name(settings, name)}.py"


def config_path(settings: "Settings", name: str) -> Path:
    return settings.custom_tools_dir / f"{files_name(settings, name)}.toml"


def draft_path(settings: "Settings", name: str) -> Path:
    """The `.py` written by `--template`, which the loader skips until it is renamed."""
    return settings.custom_tools_dir / f"_{files_name(settings, name)}.py"


def off_path(settings: "Settings", name: str) -> Path:
    return settings.custom_tools_dir / f"{files_name(settings, name)}{OFF_SUFFIX}"


def adopt(settings: "Settings", name: str) -> bool:
    """Rename a plugin's files from the name it had to the one it has now, in place.

    Only ever on the owner's explicit `keryx plugins install`: an older build that shares
    this tools directory finds its plugin under the old name alone. A `.py` that is still
    the stock one line is replaced by today's; one edited by hand is moved as it is, and
    goes on working, because the entry point it calls is kept. False when there was
    nothing to rename.
    """
    spec = plugin(name)
    if not spec.formerly or files_name(settings, name) != spec.formerly:
        return False
    tools = settings.custom_tools_dir
    for old, new in [(f"{spec.formerly}{suffix}", f"{spec.name}{suffix}")
                     for suffix in _SUFFIXES] + [(f"_{spec.formerly}.py", f"_{spec.name}.py")]:
        if (tools / old).exists():
            os.replace(tools / old, tools / new)
    for path in (tools / f"{spec.name}.py", tools / f"_{spec.name}.py"):
        if path.exists() and path.read_text(encoding="utf-8") == _template(
                f"{spec.formerly}.py"):
            write_private(path, _template(f"{spec.name}.py"))
    return True


# --- reading -----------------------------------------------------------------------------


def defaults(name: str) -> dict[str, Any]:
    return {key.name: _copy(key.default) for key in plugin(name).keys}


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    return list(value) if isinstance(value, list) else value


#: Whether a line a newer setting replaced, which says nothing that line ever could, is
#: read as the newer setting's default instead of refused. Only inside `mend`, which is
#: how the plugin's sign-in rewrites such a line. A context variable because the reading
#: is several calls deep.
_FORGIVING: contextvars.ContextVar[bool] = contextvars.ContextVar("forgiving", default=False)


def _current(where: str, keys: tuple[Key, ...], values: Mapping[str, Any],
             sign_in: str = "") -> dict[str, Any]:
    """`values` with each setting that was replaced (`Key.formerly`) read into the one
    that replaced it, and taken out: the newer one wins when both are there, and the
    older is never handed on to be written.

    An older line that says nothing it ever could is refused in a sentence that names
    the line and what rewrites it (`sign_in`), **never the values it used to take**:
    those are words of a setting that is gone, and the owner is not shown them. The
    sign-in rewrites it (`mend`) by reading it as nothing set, which is the newer
    setting's default.
    """
    current = dict(values)
    for key in keys:
        if not key.formerly or key.formerly not in current:
            continue
        old = current.pop(key.formerly)
        if key.name in current:
            continue
        was = key.was or {}
        said = str(old).strip().lower()
        if said in was:
            current[key.name] = _copy(was[said])
        elif not _FORGIVING.get():
            mend = f": `{sign_in}` rewrites it" if sign_in else ""
            raise PluginConfigError(
                f"{where}: the {key.formerly} line is not understood (it is from before "
                f"{key.name}, which replaced it){mend}"
            )
    return current


def clean(name: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """`values` over the defaults, each known key validated; unknown keys carried through.

    Raw values from a command line (`"40"`, `"alpha=gpu"` is the caller's business) are
    taken as they would be typed: a number may be a string of one.
    """
    spec = plugin(name)
    known = {key.name: key for key in spec.keys}
    merged = defaults(name) | _current(name, spec.keys, _gathered(spec, values), spec.sign_in)
    cleaned: dict[str, Any] = {}
    for key, value in merged.items():
        cleaned[key] = (_clean(name, known[key], value, spec.sign_in) if key in known
                        else value)
    return cleaned


def _gathered(spec: Plugin, values: Mapping[str, Any]) -> dict[str, Any]:
    """`values` with each `[<prefix>NAME]` table of a file moved under its `tables` key.

    In the file every named table is at the top level (`[workspace-work]`); in memory they
    are one dict under the key (`values["workspaces"]["work"]`), which is also how a
    caller hands them over. Both are taken, the top-level one winning.
    """
    gathered = dict(values)
    for key in spec.keys:
        if key.kind != "tables" or not key.prefix:
            continue
        nested = gathered.get(key.name)
        tables = dict(nested) if isinstance(nested, Mapping) else nested
        for item in [item for item in gathered if str(item).startswith(key.prefix)]:
            if isinstance(gathered[item], Mapping) and isinstance(tables, dict | None):
                tables = {**(tables or {}), str(item).removeprefix(key.prefix): gathered.pop(item)}
        if tables is not None:
            gathered[key.name] = tables
    return gathered


def _clean(name: str, key: Key, value: Any, sign_in: str = "") -> Any:
    where = f"{name}: {key.name}"
    if key.kind == "word":
        text = "" if value is None else str(value).strip()
        if text and not WORD.fullmatch(text):
            raise PluginConfigError(f"{where} must be a bare word (letters, digits, . _ -)")
        return text
    if key.kind == "choice":
        text = str(value).strip().lower()
        if text not in key.choices:
            raise PluginConfigError(f"{where} must be one of {', '.join(key.choices)}")
        return text
    if key.kind == "choices":
        # Any of them, as a list; a hand-typed string of them is taken too. Written back
        # in the order they are listed, each once.
        if isinstance(value, str):
            value = value.replace(",", " ").split()
        if not isinstance(value, list | tuple | set | frozenset):
            raise PluginConfigError(f"{where} must be a list of {', '.join(key.choices)}")
        picked = {str(item).strip().lower() for item in value}
        if not picked <= set(key.choices):
            raise PluginConfigError(f"{where} may hold only {', '.join(key.choices)}")
        return [choice for choice in key.choices if choice in picked]
    if key.kind == "number":
        if isinstance(value, bool):
            raise PluginConfigError(f"{where} must be a number")
        try:
            number = float(value) if value not in (None, "") else 0.0
        except (TypeError, ValueError):
            raise PluginConfigError(f"{where} must be a number") from None
        if number < 0 or (key.positive and number == 0) or number != number:
            limit = "more than 0" if key.positive else "0 or more"
            raise PluginConfigError(f"{where} must be {limit}")
        if key.maximum is not None and number > key.maximum:
            raise PluginConfigError(f"{where} must be {key.maximum:g} or less")
        return int(number) if number.is_integer() and isinstance(key.default, int) else number
    if key.kind in ("path", "line"):
        text = "" if value is None else str(value).strip()
        if any(char in text for char in "\n\r\0"):
            raise PluginConfigError(f"{where} must be one line")
        return text
    if key.kind == "tables":
        return _clean_tables(name, key, value, sign_in)
    # A table: `{alias: partition}`, both bare words, the alias lower-cased because it is
    # matched against speech.
    if not isinstance(value, Mapping):
        raise PluginConfigError(f"{where} must be a table of name = \"value\" lines")
    table: dict[str, str] = {}
    for alias, partition in value.items():
        alias, partition = str(alias).strip().lower(), str(partition).strip()
        if not (WORD.fullmatch(alias) and WORD.fullmatch(partition)):
            raise PluginConfigError(f"{where}: each name and value must be a bare word")
        table[alias] = partition
    return table


def _clean_tables(name: str, key: Key, value: Any, sign_in: str = "") -> dict[str, dict[str, Any]]:
    """`{name: {field: value}}`: each name a bare word, lower-cased because it may be
    matched against speech, and each table its fields over their defaults, validated. A
    plain value somebody added by hand is carried through."""
    where = f"{name}: {key.name}"
    if not isinstance(value, Mapping):
        raise PluginConfigError(f"{where} must be [{key.prefix}NAME] tables")
    known = {field.name: field for field in key.fields}
    tables: dict[str, dict[str, Any]] = {}
    for alias, table in value.items():
        alias = str(alias).strip().lower()
        if not WORD.fullmatch(alias) or not isinstance(table, Mapping):
            raise PluginConfigError(
                f"{where}: each one is a [{key.prefix}NAME] table, NAME a bare word"
            )
        merged = {field.name: _copy(field.default) for field in key.fields} | _current(
            f"{name}: {key.name}.{alias}", key.fields, table, sign_in)
        tables[alias] = {
            item: _clean(f"{name}: {key.name}.{alias}", known[item], each)
            if item in known else each
            for item, each in merged.items()
            if item in known or not isinstance(each, Mapping)
        }
    return tables


def read_config_file(name: str, path: Path) -> dict[str, Any]:
    """The settings in `path`, over the defaults and validated; a missing file is defaults."""
    try:
        data = read_toml(path)
    except tomllib.TOMLDecodeError as error:
        raise PluginConfigError(f"{path} does not parse ({error})") from None
    return clean(name, data)


def read_config(settings: "Settings", name: str) -> dict[str, Any]:
    """The plugin's settings: its TOML, else the copy set aside when it was turned off."""
    path = config_path(settings, name)
    if not path.exists() and off_path(settings, name).exists():
        path = off_path(settings, name)
    return read_config_file(name, path)


def mend(settings: "Settings", name: str) -> bool:
    """Rewrite the plugin's file when a line a newer setting replaced is all that stops
    it being read: that line is read as nothing set (the newer setting's default, which
    for what a workspace may do is nothing), and the file is written back without it.
    True when it was rewritten.

    The plugin's sign-in calls this before it reads anything, which is what makes the
    refusal's own advice true (`_current`). A file that reads as it is, is not touched,
    and one that still does not read is left for whatever reads it next to refuse.
    """
    path = config_path(settings, name)
    if not path.exists():
        path = off_path(settings, name)
    try:
        read_config_file(name, path)
        return False
    except PluginConfigError:
        pass
    token = _FORGIVING.set(True)
    try:
        write_private(path, render_config(name, read_config_file(name, path)))
    except PluginConfigError:
        return False
    finally:
        _FORGIVING.reset(token)
    return True


def install_problem(settings: "Settings", name: str) -> str | None:
    """Why the plugin's settings would not load, in a sentence; None when they would."""
    try:
        values = read_config(settings, name)
    except PluginConfigError as error:
        return str(error)
    return plugin(name).problem(values)


# --- writing -----------------------------------------------------------------------------


def _template(filename: str) -> str:
    """One of `templates/`: data copied into the tools directory, never imported from here.

    The `.toml` files are `str.format` templates, so a literal brace in one is doubled.
    """
    return resources.files("keryx.plugins").joinpath("templates", filename).read_text("utf-8")


def render_config(name: str, values: Mapping[str, Any]) -> str:
    """The plugin's TOML template, filled in with `values` (validated first).

    The template's comments are the documentation of every key, so they are rewritten
    every time; a key somebody added by hand that the template does not know is carried
    through underneath.
    """
    cleaned = clean(name, values)
    spec = plugin(name)
    name = spec.name
    known = {key.name for key in spec.keys}
    filled: dict[str, str] = {}
    for key in spec.keys:
        value = cleaned[key.name]
        if key.kind == "table":
            filled[key.name] = "\n".join(
                f"{toml_key(alias)} = {toml_value(partition)}" for alias, partition in value.items()
            )
        elif key.kind == "tables":
            filled[key.name] = "\n\n".join(
                f"[{toml_key(key.prefix + alias)}]\n" + "\n".join(
                    f"{toml_key(item)} = {toml_value(each)}" for item, each in table.items()
                )
                for alias, table in value.items()
            )
        else:
            filled[key.name] = toml_value(value)
    extra = {key: value for key, value in cleaned.items() if key not in known}
    scalars = [
        f"{toml_key(key)} = {toml_value(value)}"
        for key, value in extra.items()
        if not isinstance(value, dict)
    ]
    filled["extra"] = "\n".join(scalars)
    text = _template(f"{name}.toml").format(**filled)
    for table, value in extra.items():
        if isinstance(value, dict):
            text += f"\n[{toml_key(table)}]\n" + "".join(
                f"{toml_key(key)} = {toml_value(item)}\n" for key, item in value.items()
            )
    return re.sub(r"\n{3,}", "\n\n", text).rstrip("\n") + "\n"


def write_config(settings: "Settings", name: str, values: Mapping[str, Any]) -> Path:
    """Write the plugin's TOML (0600): `values` over what it holds now, comments and all."""
    current = read_config(settings, name)
    path = write_private(config_path(settings, name), render_config(name, current | dict(values)))
    off_path(settings, name).unlink(missing_ok=True)
    return path


def tool_source(name: str, settings: "Settings | None" = None) -> str:
    """The one line that is the plugin's `.py`: today's, or the one that goes with the
    name its files have on this machine, which an older build can still load."""
    spec = plugin(name)
    return _template(f"{files_name(settings, name) if settings else spec.name}.py")


def install(settings: "Settings", name: str, *, rename: bool = False) -> Path:
    """Turn the plugin on: settings validated, then its `.py` in place. Returns the `.py`.

    A draft (`--template`) is renamed into place, so what the owner edited is what runs; a
    `.py` already there is left as it is. The TOML is written from the aside copy or the
    defaults when there is none yet. `rename` is the owner's explicit install: files
    still under a name the plugin had before are renamed first (`adopt`).
    """
    if rename:
        adopt(settings, name)
    if problem := install_problem(settings, name):
        raise PluginConfigError(f"{name} was not turned on: {problem}")
    if not config_path(settings, name).exists():
        write_config(settings, name, {})
    target, draft = tool_path(settings, name), draft_path(settings, name)
    if draft.exists():
        os.replace(draft, target)
        write_private(target, target.read_text(encoding="utf-8"))
    elif not target.exists():
        write_private(target, tool_source(name, settings))
    return target


def write_template(settings: "Settings", name: str, values: Mapping[str, Any]) -> Path:
    """The TOML and a draft `.py`, for the owner to finish by hand. Returns the TOML."""
    path = write_config(settings, name, values)
    if not tool_path(settings, name).exists():
        write_private(draft_path(settings, name), tool_source(name, settings))
    return path


def remove(settings: "Settings", name: str) -> bool:
    """Turn the plugin off: its `.py` goes, its TOML is set aside. False when it was off."""
    plugin(name)
    was_on = tool_path(settings, name).exists()
    for path in (tool_path(settings, name), draft_path(settings, name)):
        path.unlink(missing_ok=True)
    if config_path(settings, name).exists():
        os.replace(config_path(settings, name), off_path(settings, name))
    return was_on


# --- what is on --------------------------------------------------------------------------


def is_on(settings: "Settings", name: str) -> bool:
    """Cheap: its `.py` and TOML are there and the TOML validates. Nothing is imported."""
    if not (tool_path(settings, name).is_file() and config_path(settings, name).is_file()):
        return False
    return install_problem(settings, name) is None


@dataclass(frozen=True)
class Status:
    """One plugin as `keryx plugins` and `doctor` show it."""

    name: str
    on: bool
    #: Its `.py` is there, whether or not it loads.
    installed: bool
    tool_file: Path
    config_file: Path
    #: Why its file is refused, or its settings do not load; None when it loads or is off.
    refused: str | None
    values: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "on": self.on,
            "installed": self.installed,
            "tool_file": str(self.tool_file),
            "config_file": str(self.config_file),
            "refused": self.refused,
            "settings": self.values,
        }


def status(settings: "Settings") -> list[Status]:
    """Every plugin, through the loader a call uses: an edited file still counts, and a
    refused one says why."""
    from keryx.tools.builtin import BUILTIN_TOOL_NAMES
    from keryx.tools.custom import load_custom_tools

    loaded = load_custom_tools(settings.custom_tools_dir, BUILTIN_TOOL_NAMES, settings=settings)
    tools = {tool.name for _, tool in loaded.tools}
    refusals = {path.name: why for path, why in loaded.errors}
    found = []
    for name in PLUGINS:
        values: dict[str, Any] | None = None
        refused = refusals.get(tool_path(settings, name).name)
        try:
            values = read_config(settings, name)
        except PluginConfigError as error:
            refused = refused or str(error)
        installed = tool_path(settings, name).is_file()
        found.append(
            Status(
                name=name,
                on=name in tools,
                installed=installed,
                tool_file=tool_path(settings, name),
                config_file=config_path(settings, name),
                refused=refused if installed else None,
                values=values,
            )
        )
    return found


# --- what they read ----------------------------------------------------------------------


def current_settings() -> "Settings":
    """The settings as the store holds them now, whether or not a voice key is set."""
    from pydantic import ValidationError

    from keryx.config import PLACEHOLDER_KEY, load_settings

    try:
        return load_settings()
    except ValidationError:
        return load_settings(openai_api_key=PLACEHOLDER_KEY)


def loading_settings() -> "Settings":
    """The settings of whoever is loading the tools directory, else the store's now."""
    from keryx.tools.custom import LOADING_SETTINGS

    return LOADING_SETTINGS.get() or current_settings()


def secret(settings: "Settings", name: str) -> str | None:
    """A secret field: from `settings`, else from the store now.

    The second half is what lets a key saved after `keryx serve` started reach a plugin
    turned on since, with no restart.
    """
    value = getattr(settings, name, None)
    if value:
        return value
    with contextlib.suppress(Exception):
        return getattr(current_settings(), name, None) or None
    return None


# --- the command line --------------------------------------------------------------------


def parse_assignment(text: str, *, what: str = "KEY=VALUE") -> tuple[str, str]:
    """`key=value` from a command line, or a `PluginConfigError` saying the shape wanted."""
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        raise PluginConfigError(f"{text!r} is not {what}")
    return key.strip(), value.strip()


def command_line_values(name: str, assignments: list[str]) -> dict[str, Any]:
    """`--set KEY=VALUE` for plugin `name`: its own keys only, and never a secret.

    A secret named here is refused with the command that does take it, because a value on
    a command line lands in shell history and the process list.
    """
    spec = plugin(name)
    keys = {key.name: key for key in spec.keys}
    # A setting that was replaced is refused as the one that replaced it would be.
    settled = {key.name for key in spec.keys if key.by_sign_in} | {
        key.formerly for key in spec.keys if key.by_sign_in and key.formerly}
    values: dict[str, Any] = {}
    for text in assignments:
        key, value = parse_assignment(text)
        if key.upper() in spec.secrets or key.lower() in {s.lower() for s in spec.secrets}:
            raise PluginConfigError(
                f"{key.upper()} is a secret and is never taken on a command line: "
                f"`keryx config set {key.upper()} --stdin`"
            )
        if key in settled:
            raise PluginConfigError(
                f"{key} is settled by `{spec.sign_in}` when the workspace is connected, from "
                "what Slack says of its tokens, and is never set by hand"
            )
        if key not in keys or keys[key].kind in TABLE_KINDS:
            known = ", ".join(k for k, spec_key in keys.items()
                              if spec_key.kind not in TABLE_KINDS and not spec_key.by_sign_in)
            hint = "; its secrets: " + ", ".join(spec.secrets) if spec.secrets else ""
            if spec.google:
                hint = "; its sign-in is `keryx auth login google`"
            raise PluginConfigError(f"{name} has no setting {key!r} (it has {known}{hint})")
        values[key] = value
    cleaned = clean(name, values)
    return {key: cleaned[key] for key in values}
