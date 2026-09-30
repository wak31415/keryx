"""Plugins: the optional voice tools, installed into the owner's tools directory when wanted.

Four tools are not Keryx's to offer on every machine — `send_to_slack`, `check_email`,
`check_billing` and `cluster_stats` each need something only some owners have — so none is
registered from the source tree. A plugin exists when the owner turns it on, and it is then
an ordinary custom tool (`keryx.tools.custom`): two files in `DATA_DIR/tools`,

- `<name>.py`, one line calling `keryx.plugins.<module>.<name>_tool`, so the logic stays
  here with its tests and a fix reaches every installed copy; and
- `<name>.toml`, the plugin's settings, commented, which the owner may edit by hand and the
  wizard edits in place.

Its secrets are not in that file: a token or an admin key stays in `secrets.toml` through
the store (`keryx config set KEY --stdin`), and a Gmail sign-in in its own token file.

`PLUGINS` is the table everything else reads, in the style of `agents.registry.BACKENDS`:
what each is called, what the wizard asks (`important`; the rest stay in the file, with
their comments), which secrets it uses, and which of the settings it replaced
(`retired`) — the upgrade path from before 2026-09-29, when these were settings of their
own. `keryx plugins` and the Plugins section of `keryx setup` are the two ways in.

Every file is written through `config.files.write_private`, 0600, because the loader refuses
a file anyone else could write. A value is validated before it is written (bare words,
choices, numbers) and quoted by `toml_value`, so nothing written can end its own line.
"""

import contextlib
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from keryx.config.files import read_toml, toml_key, toml_value, write_private

if TYPE_CHECKING:  # pragma: no cover - typing only
    from keryx.config import Settings
    from keryx.config.store import ConfigStore

#: What a word in a plugin's settings has to be: a cluster alias or partition, a channel id,
#: a model name. The first two reach a remote shell, so anything else is refused on write.
WORD = re.compile(r"[A-Za-z0-9_.-]+")

#: Where a turned-off plugin's settings wait, so turning it back on remembers them.
OFF_SUFFIX = ".toml.off"

Kind = Literal["word", "choice", "number", "path", "table"]


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
    #: The settings it replaced: `{OLD_KEY: toml key}`.
    retired: Mapping[str, str] = field(default_factory=dict)
    #: What must hold before it can be installed, beyond each key being valid.
    problem: Callable[[dict[str, Any]], str | None] = lambda _values: None

    def key(self, name: str) -> Key:
        return next(key for key in self.keys if key.name == name)


def _slack_problem(values: dict[str, Any]) -> str | None:
    if values["channel_id"] or values["mcp_server"]:
        return None
    return "channel_id is empty (or name an mcp_server whose config has one)"


def _cluster_problem(values: dict[str, Any]) -> str | None:
    return None if values["clusters"] else "[clusters] names no host"


PLUGINS: dict[str, Plugin] = {
    "send_to_slack": Plugin(
        name="send_to_slack",
        title="Slack",
        summary='"Send me that on Slack" posts to your DM; PIN lockouts are posted there too',
        default=True,
        important=("channel_id",),
        secrets=("SLACK_BOT_TOKEN",),
        keys=(
            Key("channel_id", "word", "", "The DM channel id (in the conversation's details)"),
            Key("mcp_server", "word", "",
                "The user-scope MCP server in ~/.claude.json that gives subagents Slack"),
        ),
        module="slack",
        retired={"SLACK_CHANNEL_ID": "channel_id", "SLACK_MCP_SERVER": "mcp_server"},
        problem=_slack_problem,
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
        retired={"EMAIL_MODEL": "model", "EMAIL_EFFORT": "effort"},
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
        retired={
            "BILLING_PROVIDER": "provider",
            "BILLING_MONTHLY_BUDGET": "monthly_budget",
            "OPENAI_BILLING_PROJECT_ID": "openai_project_id",
            "OPENAI_BILLING_API_KEY_ID": "openai_api_key_id",
            "ANTHROPIC_BILLING_WORKSPACE_ID": "anthropic_workspace_id",
        },
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
        retired={
            "CLUSTERS": "clusters",
            "CLUSTER_SSH_GUARD": "guard",
            "CLUSTER_QUERY_TIMEOUT_S": "timeout_s",
        },
        problem=_cluster_problem,
    ),
}

#: Every setting a plugin replaced. `serve` ignores them; `doctor` names them.
RETIRED_KEYS = frozenset(key for plugin in PLUGINS.values() for key in plugin.retired)


def plugin(name: str) -> Plugin:
    """The plugin called `name`, or a `PluginConfigError` naming the ones there are."""
    try:
        return PLUGINS[name]
    except KeyError:
        raise PluginConfigError(
            f"there is no plugin called {name!r} ({', '.join(PLUGINS)})"
        ) from None


# --- where the files are -----------------------------------------------------------------


def tool_path(settings: "Settings", name: str) -> Path:
    return settings.custom_tools_dir / f"{name}.py"


def config_path(settings: "Settings", name: str) -> Path:
    return settings.custom_tools_dir / f"{name}.toml"


def draft_path(settings: "Settings", name: str) -> Path:
    """The `.py` written by `--template`, which the loader skips until it is renamed."""
    return settings.custom_tools_dir / f"_{name}.py"


def off_path(settings: "Settings", name: str) -> Path:
    return settings.custom_tools_dir / f"{name}{OFF_SUFFIX}"


# --- reading -----------------------------------------------------------------------------


def defaults(name: str) -> dict[str, Any]:
    return {key.name: _copy(key.default) for key in plugin(name).keys}


def _copy(value: Any) -> Any:
    return dict(value) if isinstance(value, dict) else value


def clean(name: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """`values` over the defaults, each known key validated; unknown keys carried through.

    Raw values from a command line (`"40"`, `"alpha=gpu"` is the caller's business) are
    taken as they would be typed: a number may be a string of one.
    """
    spec = plugin(name)
    known = {key.name: key for key in spec.keys}
    merged = defaults(name) | dict(values)
    cleaned: dict[str, Any] = {}
    for key, value in merged.items():
        cleaned[key] = _clean(name, known[key], value) if key in known else value
    return cleaned


def _clean(name: str, key: Key, value: Any) -> Any:
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
        return int(number) if number.is_integer() and isinstance(key.default, int) else number
    if key.kind == "path":
        text = "" if value is None else str(value).strip()
        if any(char in text for char in "\n\r\0"):
            raise PluginConfigError(f"{where} must be one line")
        return text
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
    known = {key.name for key in spec.keys}
    filled: dict[str, str] = {}
    for key in spec.keys:
        value = cleaned[key.name]
        if key.kind == "table":
            filled[key.name] = "\n".join(
                f"{toml_key(alias)} = {toml_value(partition)}" for alias, partition in value.items()
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


def tool_source(name: str) -> str:
    return _template(f"{name}.py")


def install(settings: "Settings", name: str) -> Path:
    """Turn the plugin on: settings validated, then its `.py` in place. Returns the `.py`.

    A draft (`--template`) is renamed into place, so what the owner edited is what runs; a
    `.py` already there is left as it is. The TOML is written from the aside copy or the
    defaults when there is none yet.
    """
    if problem := install_problem(settings, name):
        raise PluginConfigError(f"{name} was not turned on: {problem}")
    if not config_path(settings, name).exists():
        write_config(settings, name, {})
    target, draft = tool_path(settings, name), draft_path(settings, name)
    if draft.exists():
        os.replace(draft, target)
        write_private(target, target.read_text(encoding="utf-8"))
    elif not target.exists():
        write_private(target, tool_source(name))
    return target


def write_template(settings: "Settings", name: str, values: Mapping[str, Any]) -> Path:
    """The TOML and a draft `.py`, for the owner to finish by hand. Returns the TOML."""
    path = write_config(settings, name, values)
    if not tool_path(settings, name).exists():
        write_private(draft_path(settings, name), tool_source(name))
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
        refused = refusals.get(f"{name}.py")
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


# --- from before plugins -----------------------------------------------------------------


def retired_in(store: "ConfigStore") -> dict[str, Any]:
    """Every retired setting the store still holds, by key."""
    stored = store.stored()
    return {key: stored[key] for key in sorted(RETIRED_KEYS) if key in stored}


def from_retired(name: str, stored: Mapping[str, Any]) -> dict[str, Any]:
    """The plugin's TOML values its retired settings imply."""
    values = {}
    for old, key in plugin(name).retired.items():
        if old in stored and stored[old] not in (None, ""):
            values[key] = stored[old]
    return values


def was_offered(settings: "Settings", store: "ConfigStore", name: str) -> bool:
    """Whether the tool was offered before it was a plugin: what `--from-settings` moves.

    Retired settings for it, or for the two that took none, what made them appear: a Gmail
    sign-in, an admin key.
    """
    if from_retired(name, retired_in(store)):
        return True
    if name == "check_email":
        from keryx.integrations.gmail import token_path

        return token_path(settings).is_file()
    if name == "check_billing":
        return bool(settings.openai_admin_key or settings.anthropic_admin_key)
    return False


@dataclass(frozen=True)
class Moved:
    """What `move_from_settings` did for one plugin: its TOML, and why it is not on if not."""

    name: str
    config_file: Path
    values: dict[str, Any]
    problem: str | None


def move_from_settings(
    settings: "Settings", store: "ConfigStore", names: list[str] | None = None
) -> list[Moved]:
    """Write each plugin's TOML from the settings it replaced, turn it on, and drop them.

    `names` None is every plugin that was offered before (`was_offered`). A retired setting
    leaves the store only once its plugin's TOML holds its value, so a plugin that could not
    be turned on keeps its old settings for the next try.
    """
    retired = retired_in(store)
    chosen = names if names is not None else [
        name for name in PLUGINS if was_offered(settings, store, name)
    ]
    moved: list[Moved] = []
    dropping: list[str] = []
    for name in chosen:
        values = from_retired(name, retired)
        path = write_config(settings, name, values)
        dropping += [key for key in plugin(name).retired if key in retired]
        try:
            install(settings, name)
            problem = None
        except PluginConfigError as error:
            problem = str(error)
        moved.append(Moved(name, path, values, problem))
    if dropping:
        store.drop_retired(dropping)
    return moved


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
    values: dict[str, Any] = {}
    for text in assignments:
        key, value = parse_assignment(text)
        if key.upper() in spec.secrets or key.lower() in {s.lower() for s in spec.secrets}:
            raise PluginConfigError(
                f"{key.upper()} is a secret and is never taken on a command line: "
                f"`keryx config set {key.upper()} --stdin`"
            )
        if key not in keys or keys[key].kind == "table":
            known = ", ".join(k for k, spec_key in keys.items() if spec_key.kind != "table")
            hint = "; its secrets: " + ", ".join(spec.secrets) if spec.secrets else ""
            if name == "check_email":
                hint = "; the Gmail sign-in is `keryx auth login gmail`"
            raise PluginConfigError(f"{name} has no setting {key!r} (it has {known}{hint})")
        values[key] = value
    cleaned = clean(name, values)
    return {key: cleaned[key] for key in values}
