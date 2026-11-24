"""A plugin's two files: its TOML (commented, validated, edited in place) and its `.py`."""

import importlib.util
import stat
import tomllib
from importlib import resources
from pathlib import Path

import pytest

from keryx import plugins
from keryx.tools.custom import ToolUnavailable
from plugins.helpers import loading, names, turn_on


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- the TOML ----------------------------------------------------------------------------


def test_a_fresh_file_is_the_template_with_the_defaults(settings):
    path = plugins.write_config(settings, "check_billing", {})

    text = path.read_text()
    assert "keryx config set OPENAI_ADMIN_KEY --stdin" in text
    assert tomllib.loads(text) == plugins.defaults("check_billing")
    assert mode(path) == 0o600


def test_the_comments_survive_every_rewrite(settings):
    plugins.write_config(settings, "cluster_stats", {"clusters": {"alpha": "gpu"}})
    path = plugins.write_config(settings, "cluster_stats", {"timeout_s": 30})

    text = path.read_text()
    assert "# One line per host" in text and "ControlMaster" in text
    assert tomllib.loads(text) == {
        "guard": "", "timeout_s": 30.0, "clusters": {"alpha": "gpu"},
    }


def test_keys_added_by_hand_are_carried_through(settings):
    path = plugins.write_config(settings, "slack_report", {"channel_id": "D1"})
    path.write_text(path.read_text() + 'note = "mine"\n\n[extra]\nkept = 1\n')

    plugins.write_config(settings, "slack_report", {"channel_id": "D2"})

    data = tomllib.loads(path.read_text())
    assert data["channel_id"] == "D2"
    assert data["note"] == "mine" and data["extra"] == {"kept": 1}


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("slack_report", {"channel_id": 'D1"\nmcp_server = "evil'}),
        ("slack_report", {"mcp_server": "a b"}),
        ("check_email", {"effort": "extreme"}),
        ("check_billing", {"monthly_budget": -1}),
        ("check_billing", {"monthly_budget": True}),
        ("cluster_stats", {"timeout_s": 0}),
        ("cluster_stats", {"guard": "/bin/x\n[clusters]"}),
        ("cluster_stats", {"clusters": ["alpha"]}),
        ("slack_report", {"allow": ["everything"]}),
        ("slack_report", {"allow": "converse"}),
        ("slack_report", {"allow": 3}),
        ("slack_report", {"owner_id": "U1 or U2"}),
        ("slack_report", {"team": "Acme\n[workspaces.evil]"}),
        ("slack_report", {"workspaces": ["work"]}),
        ("slack_report", {"workspaces": {"work": "U1"}}),
        ("slack_report", {"workspaces": {"my work": {}}}),
        ("slack_report", {"workspaces": {"work": {"allow": ["all"]}}}),
        ("slack_report", {"workspaces": {"work": {"access": "all"}}}),
        ("slack_report", {"workspaces": {"work": {"channel_id": 'U1"\nx = "y'}}}),
    ],
)
def test_a_value_that_is_not_its_kind_is_refused_before_anything_is_written(
    settings, name, values
):
    with pytest.raises(plugins.PluginConfigError):
        plugins.write_config(settings, name, values)

    assert not plugins.config_path(settings, name).exists()


def test_numbers_arrive_as_text_from_a_command_line(settings):
    assert plugins.clean("check_billing", {"monthly_budget": "40"})["monthly_budget"] == 40
    assert plugins.clean("check_billing", {"monthly_budget": "40.5"})["monthly_budget"] == 40.5
    assert plugins.clean("check_billing", {"monthly_budget": ""})["monthly_budget"] == 0


def test_there_is_no_plugin_by_another_name():
    with pytest.raises(plugins.PluginConfigError, match="no plugin called"):
        plugins.plugin("send_fax")


# --- on and off --------------------------------------------------------------------------


def test_installing_writes_the_one_line_template_privately(settings):
    turn_on(settings, "check_billing")

    path = plugins.tool_path(settings, "check_billing")
    assert "check_billing_tool(Path(__file__).with_suffix" in path.read_text()
    assert mode(path) == 0o600
    assert plugins.is_on(settings, "check_billing")


def test_removing_sets_the_settings_aside_and_turning_it_on_again_keeps_them(settings):
    turn_on(settings, "check_billing", monthly_budget=75)

    assert plugins.remove(settings, "check_billing") is True
    assert not plugins.tool_path(settings, "check_billing").exists()
    assert not plugins.is_on(settings, "check_billing")
    assert plugins.read_config(settings, "check_billing")["monthly_budget"] == 75

    plugins.install(settings, "check_billing")
    assert plugins.read_config(settings, "check_billing")["monthly_budget"] == 75
    assert not plugins.off_path(settings, "check_billing").exists()
    assert plugins.remove(settings, "slack_report") is False


def test_a_template_is_written_off_and_turned_on_as_it_was_edited(settings):
    plugins.write_template(settings, "cluster_stats", {})
    draft = plugins.draft_path(settings, "cluster_stats")

    assert draft.exists() and not plugins.tool_path(settings, "cluster_stats").exists()
    assert "cluster_stats" not in names(settings)  # a draft is never loaded
    with pytest.raises(plugins.PluginConfigError, match="names no host"):
        plugins.install(settings, "cluster_stats")

    path = plugins.config_path(settings, "cluster_stats")
    path.write_text(path.read_text().replace("[clusters]\n", '[clusters]\nalpha = "gpu"\n'))
    draft.write_text(draft.read_text() + "# edited\n")
    plugins.install(settings, "cluster_stats")

    tool = plugins.tool_path(settings, "cluster_stats")
    assert not draft.exists() and tool.read_text().endswith("# edited\n")
    assert mode(tool) == 0o600
    assert "cluster_stats" in names(settings)


def test_an_edited_py_is_left_as_it_is_on_a_second_install(settings):
    turn_on(settings, "check_billing")
    path = plugins.tool_path(settings, "check_billing")
    path.write_text(path.read_text() + "# mine\n")

    plugins.install(settings, "check_billing")

    assert path.read_text().endswith("# mine\n")


def test_status_goes_through_the_loader_a_call_uses(settings):
    turn_on(settings, "check_billing")
    turn_on(settings, "check_email")  # not signed in: refused
    plugins.write_config(settings, "slack_report", {"channel_id": "D1"})  # off

    found = {status.name: status for status in plugins.status(settings)}

    assert found["check_billing"].on and found["check_billing"].refused is None
    assert not found["check_email"].on and "keryx auth login google" in found["check_email"].refused
    assert not found["slack_report"].installed and found["slack_report"].refused is None
    assert found["slack_report"].values["channel_id"] == "D1"
    assert found["cluster_stats"].as_dict()["on"] is False


def test_status_reports_a_file_whose_settings_do_not_parse(settings):
    turn_on(settings, "check_billing")
    plugins.config_path(settings, "check_billing").write_text("provider = \n")

    [billing] = [one for one in plugins.status(settings) if one.name == "check_billing"]

    assert "does not parse" in billing.refused and billing.values is None


@pytest.mark.parametrize("name", list(plugins.PLUGINS))
def test_every_template_runs_from_where_it_ships(settings, name):
    """Each `.py` template, imported from the package — what an install copies, line for line.

    Beside it is no settings file, so each either builds its tool on the defaults or refuses
    with a reason, never a traceback.
    """
    source = Path(str(resources.files("keryx.plugins").joinpath("templates", f"{name}.py")))
    spec = importlib.util.spec_from_file_location(f"template_{name}", source)
    module = importlib.util.module_from_spec(spec)

    try:
        with loading(settings):
            spec.loader.exec_module(module)
    except ToolUnavailable as refused:
        assert str(refused)
    else:
        assert getattr(module, name).name == name
    assert plugins.tool_source(name) == source.read_text()


# --- the command line --------------------------------------------------------------------


def test_command_line_values_are_its_own_keys_cleaned():
    values = plugins.command_line_values("check_billing", ["monthly_budget=40", "provider=Openai"])

    assert values == {"monthly_budget": 40, "provider": "openai"}


@pytest.mark.parametrize(
    ("name", "assignment", "says"),
    [
        ("slack_report", "SLACK_BOT_TOKEN=xoxb-1", "keryx config set SLACK_BOT_TOKEN --stdin"),
        ("check_billing", "openai_admin_key=sk", "keryx config set OPENAI_ADMIN_KEY --stdin"),
        ("slack_report", "bot_token=xoxb-1", "its secrets: SLACK_BOT_TOKEN"),
        ("check_email", "token=x", "keryx auth login google"),
        ("cluster_stats", "clusters=alpha", "has no setting"),
        ("check_billing", "budget", "is not KEY=VALUE"),
    ],
)
def test_a_secret_or_a_stranger_is_refused_with_what_to_do(name, assignment, says):
    with pytest.raises(plugins.PluginConfigError, match=None) as raised:
        plugins.command_line_values(name, [assignment])

    assert says in str(raised.value)


# --- a table of named tables: the Slack workspaces ----------------------------------------


def test_each_further_slack_workspace_is_a_table_of_the_same_settings(settings):
    work = {"allow": ["requests"], "owner_id": "U0WORK", "team": "Acme Inc",
            "channel_id": "U0WORK"}
    path = plugins.write_config(
        settings, "slack_report", {"channel_id": "U0ME", "workspaces": {"Work": work}}
    )

    data = tomllib.loads(path.read_text())
    # In the file each is a table of its own, one level deep.
    assert data["workspace-work"] == {**work, "team_id": "", "bot_user_id": ""}
    assert "workspaces" not in data
    assert (data["channel_id"], data["allow"], data["owner_id"]) == ("U0ME", [], "")
    assert "# Further workspaces, one [workspace-NAME] table each" in path.read_text()
    assert plugins.read_config(settings, "slack_report")["workspaces"]["work"]["team"] == (
        "Acme Inc"
    )


def test_a_workspace_table_survives_a_rewrite_with_what_was_added_by_hand(settings):
    path = plugins.write_config(
        settings, "slack_report", {"channel_id": "U0ME", "workspaces": {"work": {}}}
    )
    path.write_text(path.read_text() + 'note = "mine"\n')

    plugins.write_config(settings, "slack_report", {"allow": ["requests"]})

    data = tomllib.loads(path.read_text())
    assert data["allow"] == ["requests"]
    assert data["workspace-work"] == {
        "channel_id": "", "allow": [], "owner_id": "", "team": "", "team_id": "",
        "bot_user_id": "", "note": "mine",
    }


def test_what_is_ticked_is_a_list_written_in_one_order_each_once(settings):
    path = plugins.write_config(settings, "slack_report", {
        "channel_id": "U0ME", "allow": ["send", "Requests", "send"],
        "workspaces": {"work": {"allow": "read, requests"}},  # typed by hand, as a string
    })

    assert 'allow = ["requests", "send"]' in path.read_text()
    values = plugins.read_config(settings, "slack_report")
    assert values["allow"] == ["requests", "send"]
    assert values["workspaces"]["work"]["allow"] == ["requests", "read"]
    assert plugins.defaults("slack_report")["allow"] == []
    plugins.defaults("slack_report")["allow"].append("read")  # a copy, never the default
    assert plugins.defaults("slack_report")["allow"] == []


@pytest.mark.parametrize(("level", "ticked"), [
    ("announce", []),
    ("converse", ["requests"]),
    ("read", ["requests", "read"]),
    ("write", ["requests", "read", "send"]),
])
def test_a_file_that_still_says_a_level_is_read_as_what_that_level_was(settings, level, ticked):
    """`access` was one of four ordered levels until 2026-11-24. A file from then is read
    as the set that level held, with nothing asked of the owner."""
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text(
        f'channel_id = "U0ME"\naccess = "{level}"\n\n[workspace-work]\naccess = "{level}"\n'
    )

    values = plugins.read_config(settings, "slack_report")

    assert values["allow"] == ticked
    assert values["workspaces"]["work"]["allow"] == ticked
    assert "access" not in values and "access" not in values["workspaces"]["work"]


def test_the_level_is_read_and_never_written_back(settings):
    """The first write after reading it writes what is ticked, and the old key is gone
    from the top of the file and from every workspace's table: nothing carries it
    through as a setting somebody added by hand."""
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    path = plugins.config_path(settings, "slack_report")
    path.write_text('channel_id = "U0ME"\naccess = "converse"\nnote = "mine"\n'
                    '\n[workspace-work]\naccess = "read"\n')

    plugins.write_config(settings, "slack_report", {"team": "Acme"})

    data = tomllib.loads(path.read_text())
    assert data["allow"] == ["requests"] and data["note"] == "mine"
    assert data["workspace-work"]["allow"] == ["requests", "read"]
    assert "access" not in data and "access" not in data["workspace-work"]
    assert "\naccess" not in path.read_text()


def test_what_is_ticked_wins_over_a_level_an_older_build_wrote_beside_it(settings):
    """Several builds share one tools directory here. One from before the boxes writes
    `access` back whenever it saves; the newer key beside it is the one that counts."""
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text(
        'channel_id = "U0ME"\naccess = "write"\nallow = ["read"]\n'
        '\n[workspace-work]\nallow = []\naccess = "nonsense"\n'
    )

    values = plugins.read_config(settings, "slack_report")

    assert values["allow"] == ["read"]
    assert values["workspaces"]["work"]["allow"] == []


def test_a_level_that_was_never_one_is_refused_without_naming_the_levels_there_were(settings):
    """The four levels are words of a setting that is gone, and the owner is never shown
    them: the sentence names the line and the one command that rewrites it."""
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text(
        'channel_id = "U0ME"\naccess = "everything"\n')

    with pytest.raises(plugins.PluginConfigError) as refused:
        plugins.read_config(settings, "slack_report")

    assert str(refused.value) == (
        "slack_report: the access line is not understood (it is from before allow, which "
        "replaced it): `keryx auth login slack` rewrites it")
    for level in ("announce", "converse", "everything", "one of"):
        assert level not in str(refused.value)


def test_such_a_line_in_a_further_workspaces_table_is_named_the_same_way(settings):
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text(
        'channel_id = "U0ME"\n\n[workspace-work]\naccess = "reed"\n')

    with pytest.raises(plugins.PluginConfigError) as refused:
        plugins.read_config(settings, "slack_report")

    assert str(refused.value) == (
        "slack_report: workspaces.work: the access line is not understood (it is from "
        "before allow, which replaced it): `keryx auth login slack` rewrites it")


def test_mending_rewrites_a_line_that_is_not_understood_as_nothing_ticked(settings):
    """What the sign-in does first, which is what makes the refusal's advice true: the
    line is read as nothing ticked, everything else in the file is kept, and a file that
    reads as it is, is not touched."""
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    path = plugins.config_path(settings, "slack_report")
    path.write_text('channel_id = "U0ME"\naccess = "reed"\nowner_id = "U0ME"\n'
                    '\n[workspace-work]\naccess = "reed"\nchannel_id = "C0WORK"\n')

    assert plugins.mend(settings, "slack_report") is True

    values = plugins.read_config(settings, "slack_report")
    assert (values["allow"], values["channel_id"], values["owner_id"]) == ([], "U0ME", "U0ME")
    assert values["workspaces"]["work"] == {
        **values["workspaces"]["work"], "allow": [], "channel_id": "C0WORK"}
    assert not any(line.startswith("access") for line in path.read_text().splitlines())
    before = path.read_text()
    assert plugins.mend(settings, "slack_report") is False and path.read_text() == before


def test_mending_leaves_a_level_that_is_understood_and_a_file_broken_some_other_way(settings):
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    path = plugins.config_path(settings, "slack_report")
    path.write_text('channel_id = "U0ME"\naccess = "converse"\n')
    assert plugins.mend(settings, "slack_report") is False
    assert 'access = "converse"' in path.read_text()

    path.write_text('channel_id = "not a word!"\naccess = "reed"\n')
    assert plugins.mend(settings, "slack_report") is False
    assert 'access = "reed"' in path.read_text()

    path.unlink()
    assert plugins.mend(settings, "slack_report") is False and not path.exists()


def test_mending_a_plugin_that_is_off_rewrites_the_copy_set_aside(settings):
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    aside = plugins.off_path(settings, "slack_report")
    aside.write_text('channel_id = "U0ME"\naccess = "reed"\n')

    assert plugins.mend(settings, "slack_report") is True

    assert not plugins.config_path(settings, "slack_report").exists()
    assert plugins.read_config(settings, "slack_report")["allow"] == []


def test_a_slack_file_from_before_workspaces_reads_as_one_with_nothing_ticked(settings):
    """What every existing install has: two keys, and nothing to migrate."""
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text(
        'channel_id = "D123"\nmcp_server = ""\n'
    )

    values = plugins.read_config(settings, "slack_report")

    assert values == {"channel_id": "D123", "allow": [], "owner_id": "", "team": "",
                      "team_id": "", "bot_user_id": "", "mcp_server": "",
                      "workspaces": {}}
    assert plugins.install_problem(settings, "slack_report") is None


def test_a_table_is_never_set_from_a_command_line():
    with pytest.raises(plugins.PluginConfigError, match="has no setting 'clusters'"):
        plugins.command_line_values("cluster_stats", ["clusters=alpha"])
    with pytest.raises(plugins.PluginConfigError, match="settled by `keryx auth login slack`"):
        plugins.command_line_values("slack_report", ["workspaces=work"])
    assert plugins.command_line_values("slack_report", ["channel_id=C0X"]) == {
        "channel_id": "C0X"
    }
    # What is ticked is the sign-in's to write, and the level it replaced is no setting.
    with pytest.raises(plugins.PluginConfigError, match="settled by `keryx auth login slack`"):
        plugins.command_line_values("slack_report", ["allow=read"])
    with pytest.raises(plugins.PluginConfigError, match="settled by `keryx auth login slack`"):
        plugins.command_line_values("slack_report", ["access=read"])


def test_what_a_sign_in_settles_is_named_as_such_and_left_out_of_what_may_be_set():
    keys = {key.name: key.by_sign_in for key in plugins.plugin("slack_report").keys}

    assert keys == {"channel_id": False, "allow": True, "owner_id": True, "team": True,
                    "team_id": True, "bot_user_id": True, "mcp_server": False,
                    "workspaces": True}
    with pytest.raises(plugins.PluginConfigError) as unknown:
        plugins.command_line_values("slack_report", ["nonsense=1"])
    assert "(it has channel_id, mcp_server;" in str(unknown.value)


# --- an older build rewriting the same file -------------------------------------------------


def _older_write(path, **changes) -> None:
    """What the build before workspaces does to `slack_report.toml` when it saves: its
    two keys into its own template, every plain key it does not know under them, and every
    table it does not know one level deep, each value as that build wrote a value."""
    from keryx.config.files import _toml_string, toml_key, write_private

    def value(item):
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int | float):
            return repr(item)
        if isinstance(item, list | tuple):
            return "[" + ", ".join(value(each) for each in item) + "]"
        return _toml_string(str(item))  # a table here is where the older build lost data

    data = tomllib.loads(path.read_text()) | changes
    known = ("channel_id", "mcp_server")
    lines = [f"{key} = {value(data.get(key, ''))}" for key in known]
    lines += [f"{toml_key(key)} = {value(item)}" for key, item in data.items()
              if key not in known and not isinstance(item, dict)]
    for table, items in data.items():
        if isinstance(items, dict):
            lines += ["", f"[{toml_key(table)}]"]
            lines += [f"{toml_key(key)} = {value(item)}" for key, item in items.items()]
    write_private(path, "\n".join(lines) + "\n")


def test_an_older_build_rewriting_the_slack_file_keeps_every_workspace(settings):
    """Several worktrees share one tools directory on this machine. A build from before
    workspaces rewrites the file when its own setup saves a channel: what is ticked, the
    owner and every further workspace have to come through, in shapes the newer build
    reads. A list is one of them: that build's `toml_value` writes one as a list."""
    work = {"channel_id": "U0W", "allow": ["requests", "read"], "owner_id": "U0W",
            "team": "Acme Inc", "team_id": "T0W", "bot_user_id": "U0WBOT"}
    path = plugins.write_config(settings, "slack_report", {
        "channel_id": "U0OWNER", "allow": ["requests"], "owner_id": "U0OWNER", "team_id": "T0A",
        "workspaces": {"work": work},
    })
    plugins.install(settings, "slack_report")

    _older_write(path, channel_id="D0NEW")

    assert plugins.is_on(settings, "slack_report")
    values = plugins.read_config(settings, "slack_report")
    assert values["workspaces"] == {"work": work}
    assert (values["channel_id"], values["allow"], values["owner_id"], values["team_id"]) == (
        "D0NEW", ["requests"], "U0OWNER", "T0A"
    )


def test_a_file_with_workspaces_nested_under_one_table_is_still_read(settings):
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text(
        'channel_id = "U0ME"\n\n[workspaces.work]\nallow = ["requests"]\n'
        '\n[workspace-lab]\nallow = ["read"]\n'
    )

    values = plugins.read_config(settings, "slack_report")

    assert {name: each["allow"] for name, each in values["workspaces"].items()} == {
        "work": ["requests"], "lab": ["read"],
    }


def test_a_key_an_earlier_cut_wrote_and_nothing_reads_now_is_carried_and_harmless(settings):
    """`url` was stored for a while on this branch: a file that has it still loads."""
    settings.custom_tools_dir.mkdir(parents=True, exist_ok=True)
    plugins.config_path(settings, "slack_report").write_text(
        'channel_id = "U0ME"\nurl = "https://acme.slack.com/"\n'
    )

    assert plugins.install_problem(settings, "slack_report") is None
    assert plugins.read_config(settings, "slack_report")["channel_id"] == "U0ME"
