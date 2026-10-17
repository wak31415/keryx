"""`keryx plugins`: the optional voice tools."""

import json
from typing import Annotated, Any

import typer

from keryx import plugins
from keryx.cli.apps import plugins_app
from keryx.cli.common import _configure_readonly, _owner_only, _plain
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.integrations import accounts
from keryx.plugins import ssh_hosts

PLUGINS_JSON_HELP = (
    "Print one JSON document; each plugin also says whether it is `ready` — would work on a "
    "call once turned on (`on` says whether it is) — and `why` not."
)


@plugins_app.callback(invoke_without_command=True)
def plugins_command(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help=PLUGINS_JSON_HELP)] = False,
) -> None:
    """List the plugins: on or off, their two files, and why one is refused."""
    if ctx.invoked_subcommand is None:
        plugins_list(as_json)


@plugins_app.command("list")
def plugins_list(
    as_json: Annotated[bool, typer.Option("--json", help=PLUGINS_JSON_HELP)] = False,
) -> None:
    """List the plugins: on or off, their two files, and why one is refused.

    `--json` also describes each for the Mac app: its settings as typed fields, the
    accounts it needs, and `ready` — whether it would work on a call once turned on, which
    the app reads beside `on` — with `why` when it would not.
    """
    settings = _configure_readonly()
    found = plugins.status(settings)
    if as_json:
        needed = dict.fromkeys(name for spec in plugins.PLUGINS.values() for name in spec.needs)
        connected = accounts.connected(settings, needed)
        typer.echo(json.dumps({
            "directory": str(settings.custom_tools_dir),
            "plugins": [status.as_dict() | plugins.describe(status, connected)
                        for status in found],
        }, indent=2))
        return
    typer.echo(f"# {settings.custom_tools_dir}")
    for status in found:
        spec = plugins.PLUGINS[status.name]
        state = "on" if status.on else ("refused" if status.installed else "off")
        typer.echo(f"{status.name:<16} {state:<8} {spec.summary}")
        if status.refused:
            typer.echo(f"{'':<16} {status.refused}")
        if status.installed or status.config_file.exists():
            shown = {key: (status.values or {}).get(key) for key in spec.important}
            settings_line = ", ".join(f"{key}={_plain(value)}" for key, value in shown.items())
            typer.echo(f"{'':<16} {status.config_file}" + (f"  ({settings_line})"
                                                            if settings_line else ""))


@plugins_app.command("hosts")
def plugins_hosts(
    as_json: Annotated[bool, typer.Option("--json", help="Print it as one JSON document.")] = False,
) -> None:
    """The hosts in ~/.ssh/config that cluster_stats could ask, and whose master is up now.

    Only hosts with a ControlMaster are offered: Keryx rides a login you already have open,
    and never opens its own. Nothing here reaches the network (`ssh -G`, `ssh -O check`).
    """
    _configure_readonly()
    rows = []
    for host in ssh_hosts.discover():
        alive = host.control_master and ssh_hosts.master_alive(host.alias)
        rows.append({**host.as_dict(), "master_alive": alive})
    if as_json:
        typer.echo(json.dumps({"hosts": rows}, indent=2))
        return
    if not rows:
        typer.echo(f"no hosts in {ssh_hosts.default_config()}")
    for row in rows:
        master = ("up" if row["master_alive"] else "down") if row["control_master"] else "none"
        who = f"{row['user']}@{row['hostname']}" if row["user"] else row["hostname"]
        typer.echo(f"{row['alias']:<20} {who:<40} ControlMaster: {master}")


@plugins_app.command("install")
def plugins_install(
    name: Annotated[str | None, typer.Argument(help="The plugin (`keryx plugins`).")] = None,
    assignments: Annotated[
        list[str] | None,
        typer.Option("--set", help="KEY=VALUE for one of its settings; repeat for more."),
    ] = None,
    clusters: Annotated[
        list[str] | None,
        typer.Option("--cluster", help="cluster_stats: ALIAS=PARTITION, the whole [clusters] "
                     "table; repeat for more."),
    ] = None,
    guard: Annotated[
        str | None, typer.Option("--guard", help="cluster_stats: a guard script of your own.")
    ] = None,
    from_settings: Annotated[
        bool,
        typer.Option("--from-settings", help="Move the settings it replaced into its file; "
                     "with no NAME, every plugin that was offered before."),
    ] = False,
    template: Annotated[
        bool,
        typer.Option("--template", help="Write its settings file and a draft to edit, and "
                     "leave it off until `keryx plugins install NAME`."),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print {name, on, message} and nothing else.")
    ] = False,
) -> None:
    """Turn a plugin on, from its settings file, the values given here, or the old settings.

    With no values, a settings file already edited by hand (and a `--template` draft) is
    what is turned on. A secret is never taken here: `keryx config set KEY --stdin`, or
    `keryx auth login gmail`. It is offered from the next call; no restart.
    """
    _owner_only("install a plugin")
    settings = _configure_readonly()
    if as_json and from_settings:
        typer.echo("--json is for one plugin, and --from-settings may move several", err=True)
        raise typer.Exit(2)
    try:
        if from_settings:
            names = None if name is None else [plugins.plugin(name).name]
            moved = plugins.move_from_settings(settings, ConfigStore(), names)
            if not moved:
                typer.echo("nothing to move: no plugin was set up in the old settings")
            for one in moved:
                typer.echo(f"{one.name}: settings in {one.config_file}")
                if one.problem:
                    typer.echo(f"  {one.problem}")
            _echo_plugin_states(settings, [one.name for one in moved])
            return
        if name is None:
            typer.echo("which plugin? (`keryx plugins`)", err=True)
            raise typer.Exit(2)
        spec = plugins.plugin(name)
        values = _plugin_values(name, assignments, clusters, guard)
        if template:
            path = plugins.write_template(settings, name, values)
            if as_json:
                _echo_plugin_json(name, False, f"{spec.title} stays off: its settings file "
                                  "and a draft are written to finish by hand.")
            else:
                typer.echo(f"edit {path}, then `keryx plugins install {name}`")
            return
        if values:
            plugins.write_config(settings, name, values)
        plugins.install(settings, spec.name)
    except plugins.PluginConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    if not as_json:
        _echo_plugin_states(settings, [name])
        return
    status = _plugin_status(settings, name)
    if not status.on:
        typer.echo(f"{name} is installed but refused: {status.refused}", err=True)
        raise typer.Exit(1)
    _echo_plugin_json(name, True, f"{spec.title} is on from the next call.")


def _plugin_values(
    name: str, assignments: list[str] | None, clusters: list[str] | None, guard: str | None
) -> dict[str, Any]:
    """`--set KEY=VALUE`, `--cluster` and `--guard` for plugin `name`, validated as install
    validates them; a `PluginConfigError` says what is wrong."""
    values = plugins.command_line_values(name, assignments or [])
    if (clusters or guard is not None) and name != "cluster_stats":
        raise plugins.PluginConfigError("--cluster and --guard are for cluster_stats")
    if clusters:
        values["clusters"] = dict(
            plugins.parse_assignment(text, what="ALIAS=PARTITION") for text in clusters
        )
    if guard is not None:
        values["guard"] = guard
    return values


def _plugin_status(settings: Settings, name: str) -> plugins.Status:
    """The plugin as the next call would load it."""
    return next(status for status in plugins.status(settings) if status.name == name)


def _echo_plugin_json(name: str, on: bool, message: str) -> None:
    typer.echo(json.dumps({"name": name, "on": on, "message": message}, indent=2))


def _echo_plugin_states(settings: Settings, names: list[str]) -> None:
    """Whether each of `names` loads now, the way the next call will load it; exit 1 if not."""
    refused = False
    for status in plugins.status(settings):
        if status.name not in names:
            continue
        if status.on:
            typer.echo(f"{status.name} is on, from the next call")
        elif status.installed:
            refused = True
            typer.echo(f"{status.name} is installed but refused: {status.refused}")
    if refused:
        raise typer.Exit(1)


@plugins_app.command("remove")
def plugins_remove(
    name: Annotated[str, typer.Argument(help="The plugin.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask first.")] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print {name, on, message}; asks nothing first.")
    ] = False,
) -> None:
    """Turn a plugin off. Its settings are set aside, so turning it on again keeps them."""
    _owner_only("remove a plugin")
    settings = _configure_readonly()
    try:
        spec = plugins.plugin(name)
    except plugins.PluginConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    if not (yes or as_json):  # the app asks before it runs this
        typer.confirm(f"turn {name} off?", abort=True)
    was_on = plugins.remove(settings, name)
    if as_json:
        _echo_plugin_json(name, False, f"{spec.title} is off from the next call; its "
                          "settings are kept." if was_on else f"{spec.title} was not on.")
    elif was_on:
        typer.echo(f"{name} is off from the next call; its settings are kept in "
                   f"{plugins.off_path(settings, name)}")
    else:
        typer.echo(f"{name} was not on")


@plugins_app.command("set")
def plugins_set(
    name: Annotated[str, typer.Argument(help="The plugin, which must be installed.")],
    assignments: Annotated[
        list[str] | None, typer.Argument(help="KEY=VALUE for one of its settings; several.")
    ] = None,
    clusters: Annotated[
        list[str] | None,
        typer.Option("--cluster", help="cluster_stats: ALIAS=PARTITION, the whole [clusters] "
                     "table; repeat for more."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print {name, on, message} and nothing else.")
    ] = False,
) -> None:
    """Change an installed plugin's settings in its file, validated as install validates
    them. Nothing is reinstalled; a secret is never taken here. From the next call."""
    _owner_only("change a plugin's settings")
    settings = _configure_readonly()
    try:
        spec = plugins.plugin(name)
        if not (assignments or clusters):
            typer.echo(f"give KEY=VALUE for one of {name}'s settings", err=True)
            raise typer.Exit(2)
        plugins.change(settings, name, _plugin_values(name, assignments, clusters, None))
    except plugins.PluginConfigError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    status = _plugin_status(settings, name)
    if not as_json:
        typer.echo(f"{name}: settings saved" + (
            "" if status.on else f"; it does not load: {status.refused}"))
        return
    why = None if status.on else plugins.describe(
        status, accounts.connected(settings, spec.needs))["why"]
    _echo_plugin_json(name, status.on, "Settings saved." + (f" {why}" if why else ""))
