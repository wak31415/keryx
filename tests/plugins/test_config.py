"""A plugin's two files: its TOML (commented, validated, edited in place) and its `.py`."""

import importlib.util
import stat
import tomllib
from importlib import resources
from pathlib import Path

import pytest

from keryx import plugins
from keryx.config.store import ConfigStore
from keryx.integrations.gmail import token_path
from keryx.tools.custom import ToolUnavailable
from plugins.helpers import loaded, loading, names, turn_on


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
    path = plugins.write_config(settings, "send_to_slack", {"channel_id": "D1"})
    path.write_text(path.read_text() + 'note = "mine"\n\n[extra]\nkept = 1\n')

    plugins.write_config(settings, "send_to_slack", {"channel_id": "D2"})

    data = tomllib.loads(path.read_text())
    assert data["channel_id"] == "D2"
    assert data["note"] == "mine" and data["extra"] == {"kept": 1}


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("send_to_slack", {"channel_id": 'D1"\nmcp_server = "evil'}),
        ("send_to_slack", {"mcp_server": "a b"}),
        ("check_email", {"effort": "extreme"}),
        ("check_billing", {"monthly_budget": -1}),
        ("check_billing", {"monthly_budget": True}),
        ("cluster_stats", {"timeout_s": 0}),
        ("cluster_stats", {"guard": "/bin/x\n[clusters]"}),
        ("cluster_stats", {"clusters": ["alpha"]}),
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
    assert plugins.remove(settings, "send_to_slack") is False


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
    plugins.write_config(settings, "send_to_slack", {"channel_id": "D1"})  # off

    found = {status.name: status for status in plugins.status(settings)}

    assert found["check_billing"].on and found["check_billing"].refused is None
    assert not found["check_email"].on and "keryx auth login gmail" in found["check_email"].refused
    assert not found["send_to_slack"].installed and found["send_to_slack"].refused is None
    assert found["send_to_slack"].values["channel_id"] == "D1"
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


# --- from before plugins -----------------------------------------------------------------


def store_with(settings, text: str) -> ConfigStore:
    store = ConfigStore()
    store.config_path.parent.mkdir(parents=True, exist_ok=True)
    store.config_path.write_text(text)
    return store


OLD = """OPENAI_VOICE = "cedar"
SLACK_MCP_SERVER = "chat"
EMAIL_EFFORT = "medium"
CLUSTER_SSH_GUARD = "/opt/guard.sh"

[CLUSTERS]
alpha = "gpu"
"""


def test_the_retired_settings_are_found_and_mapped(settings):
    store = store_with(settings, OLD)

    assert list(plugins.retired_in(store)) == [
        "CLUSTERS", "CLUSTER_SSH_GUARD", "EMAIL_EFFORT", "SLACK_MCP_SERVER",
    ]
    assert plugins.from_retired("cluster_stats", plugins.retired_in(store)) == {
        "clusters": {"alpha": "gpu"}, "guard": "/opt/guard.sh",
    }


def test_what_was_offered_before_is_what_moves(settings):
    store = store_with(settings, OLD)
    admin = settings.model_copy(update={"openai_admin_key": "sk-admin-x"})

    assert [name for name in plugins.PLUGINS if plugins.was_offered(settings, store, name)] == [
        "send_to_slack", "check_email", "cluster_stats",
    ]
    assert plugins.was_offered(admin, store, "check_billing")
    token_path(settings).write_text("{}")
    assert plugins.was_offered(settings, store_with(settings, ""), "check_email")


def test_moving_writes_each_file_and_drops_only_what_moved(settings, tmp_path):
    store = store_with(settings, OLD.replace("/opt/guard.sh", str(tmp_path / "guard.sh")))
    (tmp_path / "guard.sh").write_text("#!/bin/sh\n")

    moved = {one.name: one for one in plugins.move_from_settings(settings, store)}

    assert set(moved) == {"send_to_slack", "check_email", "cluster_stats"}
    assert moved["cluster_stats"].problem is None
    assert plugins.read_config(settings, "check_email")["effort"] == "medium"
    assert plugins.read_config(settings, "send_to_slack")["mcp_server"] == "chat"
    assert plugins.retired_in(store) == {}
    assert tomllib.loads(store.config_path.read_text()) == {"OPENAI_VOICE": "cedar"}
    assert "cluster_stats" in {tool.name for _, tool in loaded(settings).tools}


def test_a_plugin_that_cannot_be_turned_on_still_gets_its_file(settings):
    store = store_with(settings, 'CLUSTER_QUERY_TIMEOUT_S = 9.0\n')

    [moved] = plugins.move_from_settings(settings, store, ["cluster_stats"])

    assert "names no host" in moved.problem
    assert plugins.read_config(settings, "cluster_stats")["timeout_s"] == 9.0


# --- the command line --------------------------------------------------------------------


def test_command_line_values_are_its_own_keys_cleaned():
    values = plugins.command_line_values("check_billing", ["monthly_budget=40", "provider=Openai"])

    assert values == {"monthly_budget": 40, "provider": "openai"}


@pytest.mark.parametrize(
    ("name", "assignment", "says"),
    [
        ("send_to_slack", "SLACK_BOT_TOKEN=xoxb-1", "keryx config set SLACK_BOT_TOKEN --stdin"),
        ("check_billing", "openai_admin_key=sk", "keryx config set OPENAI_ADMIN_KEY --stdin"),
        ("send_to_slack", "bot_token=xoxb-1", "its secrets: SLACK_BOT_TOKEN"),
        ("check_email", "token=x", "keryx auth login gmail"),
        ("cluster_stats", "clusters=alpha", "has no setting"),
        ("check_billing", "budget", "is not KEY=VALUE"),
    ],
)
def test_a_secret_or_a_stranger_is_refused_with_what_to_do(name, assignment, says):
    with pytest.raises(plugins.PluginConfigError, match=None) as raised:
        plugins.command_line_values(name, [assignment])

    assert says in str(raised.value)
