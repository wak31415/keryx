"""Each plugin described for the Mac app: what it does, the settings in its file (typed from
the template and `PLUGINS`), the accounts it needs, and whether it would work now.

`why` is written for a card with buttons on it, so it never carries a command.
"""

import dataclasses
import json
import tomllib

import pytest

from keryx import plugins

#: Every plugin's fields, by key: the type the app edits it as, and its choices.
FIELDS = {
    "send_to_slack": {"channel_id": ("string", None), "mcp_server": ("string", None)},
    "check_email": {"model": ("string", None),
                    "effort": ("choice", ["low", "medium", "high"])},
    "check_billing": {"provider": ("choice", ["auto", "openai", "anthropic"]),
                      "monthly_budget": ("float", None),
                      "openai_project_id": ("string", None),
                      "openai_api_key_id": ("string", None),
                      "anthropic_workspace_id": ("string", None)},
    "cluster_stats": {"clusters": ("map", None), "guard": ("string", None),
                      "timeout_s": ("float", None)},
}
NOTHING = {"slack": False, "gmail": False, "openai-admin": False, "anthropic-admin": False}


def described(settings, name, connected=NOTHING):
    status = next(one for one in plugins.status(settings) if one.name == name)
    return plugins.describe(status, connected)


@pytest.mark.parametrize("name", list(plugins.PLUGINS))
def test_every_plugin_is_described_with_its_fields_typed(settings, name):
    found = described(settings, name)

    assert set(found) == {"title", "description", "fields", "needs", "ready", "why"}
    assert found["title"] == plugins.PLUGINS[name].title
    assert found["description"].endswith(".") and len(found["description"]) < 160
    assert {f["key"]: (f["type"], f["choices"]) for f in found["fields"]} == FIELDS[name]
    for field in found["fields"]:
        assert set(field) == {"key", "title", "type", "choices", "important", "value"}
        # Hand-written, sentence case: not the key, and only its first word capitalised
        # (an acronym or a name aside).
        assert field["title"] != field["key"] and "_" not in field["title"]
        assert field["title"][0].isupper()
        assert field["important"] is (field["key"] in plugins.PLUGINS[name].important)
        assert field["value"] == plugins.defaults(name)[field["key"]]


def test_the_types_agree_with_what_the_template_writes(settings):
    """A number the template writes as a float, or one it validates as a fraction, is a float."""
    for name, fields in FIELDS.items():
        written = tomllib.loads(plugins.render_config(name, {}))
        for key, (kind, _) in fields.items():
            value = written[key]
            expected = {"string": str, "choice": str, "map": dict, "float": (int, float)}[kind]
            assert isinstance(value, expected), (name, key)
    assert plugins.clean("check_billing", {"monthly_budget": "12.5"})["monthly_budget"] == 12.5


def test_a_field_shows_the_value_in_its_file(settings):
    plugins.write_config(settings, "cluster_stats", {"clusters": {"alpha": "gpu"},
                                                     "timeout_s": 30})

    values = {f["key"]: f["value"] for f in described(settings, "cluster_stats")["fields"]}

    assert values["clusters"] == {"alpha": "gpu"} and values["timeout_s"] == 30


def test_what_each_needs_is_an_integration(settings):
    assert {name: described(settings, name)["needs"] for name in plugins.PLUGINS} == {
        "send_to_slack": ["slack"], "check_email": ["gmail"],
        "check_billing": ["openai-admin", "anthropic-admin"], "cluster_stats": [],
    }


def test_a_plugin_whose_account_is_not_connected_is_not_ready(settings):
    found = described(settings, "check_billing")

    assert found["ready"] is False
    assert found["why"] == (
        "Connect the OpenAI admin key or the Anthropic admin key in Integrations first."
    )
    ready = described(settings, "check_billing", NOTHING | {"anthropic-admin": True})
    assert ready == ready | {"ready": True, "why": None}


def test_a_connected_plugin_with_a_setting_missing_says_which(settings):
    found = described(settings, "send_to_slack", NOTHING | {"slack": True})

    assert found["ready"] is False and found["why"] == "Needs the ID of your Slack DM channel."
    assert described(settings, "cluster_stats")["why"] == "Needs a cluster host."


def test_settings_that_do_not_load_say_so(settings):
    plugins.config_path(settings, "check_email").write_text('effort = "max"\n')

    found = described(settings, "check_email", NOTHING | {"gmail": True})

    assert found["ready"] is False
    assert found["why"].startswith("Its settings file does not load:")
    assert [f["value"] for f in found["fields"]] == ["claude-opus-5-5", "low"]


def test_an_installed_plugin_the_loader_refuses_says_why_without_a_command(settings,
                                                                           tmp_path):
    plugins.write_config(settings, "cluster_stats", {"clusters": {"alpha": "gpu"},
                                                     "guard": str(tmp_path / "gone.sh")})
    plugins.install(settings, "cluster_stats")

    found = described(settings, "cluster_stats")

    assert found["ready"] is False and "gone.sh is not a file" in found["why"]
    assert "`" not in found["why"]


def test_slack_that_loads_is_ready_whatever_keryx_holds(settings):
    """Its token may be read from the MCP server's config, and it loads only with one."""
    plugins.write_config(settings, "send_to_slack", {"channel_id": "D123"})
    plugins.install(settings, "send_to_slack")
    refused = next(one for one in plugins.status(settings) if one.name == "send_to_slack")

    found = plugins.describe(refused, NOTHING)
    assert found["ready"] is False
    assert found["why"] == "Connect Slack in Integrations first."
    loaded = dataclasses.replace(refused, on=True, refused=None)
    assert plugins.describe(loaded, NOTHING)["ready"] is True


def test_a_described_plugin_carries_no_secret(settings):
    settings = settings.model_copy(update={"slack_bot_token": "xoxb-shh"})

    assert "shh" not in json.dumps(described(settings, "send_to_slack", NOTHING))


@pytest.mark.parametrize(
    ("text", "sentence"),
    [("not signed in to Gmail: `keryx auth login gmail`", "Not signed in to Gmail."),
     ("the claude CLI is not installed: `uv sync --extra claude`",
      "The claude CLI is not installed."),
     ("no bot token and channel: `keryx config set SLACK_BOT_TOKEN --stdin`, and channel_id "
      "in send_to_slack.toml", "No bot token and channel."),
     ("the guard /x is not a file (blank it for the built-in)",
      "The guard /x is not a file (blank it for the built-in).")],
)
def test_a_refusal_becomes_a_sentence_with_no_command(text, sentence):
    assert plugins.app_sentence(text) == sentence


def test_an_installed_plugin_refused_with_no_reason_of_its_own_is_not_ready(settings):
    """A refusal keyed to the tools directory (owned by somebody else) names no file."""
    plugins.install(settings, "check_billing")
    status = next(one for one in plugins.status(settings) if one.name == "check_billing")
    refused = dataclasses.replace(status, on=False, refused=None)

    found = plugins.describe(refused, NOTHING | {"openai-admin": True})

    assert found["ready"] is False and found["why"] == "Keryx can't load it; see Diagnostics."


# --- changing the settings in place -------------------------------------------------------


def test_a_change_repairs_a_file_that_does_not_validate(settings):
    path = plugins.config_path(settings, "check_billing")
    path.write_text('provider = "everyone"\nmonthly_budget = 12\n')

    plugins.write_config(settings, "check_billing", {"provider": "openai"})

    assert plugins.read_config(settings, "check_billing") == plugins.defaults(
        "check_billing") | {"provider": "openai", "monthly_budget": 12}


def test_a_file_that_does_not_parse_is_left_for_the_owner(settings):
    """Rewriting it would throw away what they were in the middle of typing."""
    path = plugins.config_path(settings, "check_billing")
    path.write_text('provider = "openai\n')

    with pytest.raises(plugins.PluginConfigError, match="does not parse"):
        plugins.write_config(settings, "check_billing", {"monthly_budget": "5"})

    assert path.read_text() == 'provider = "openai\n'


def test_a_change_that_leaves_it_invalid_writes_nothing(settings):
    path = plugins.config_path(settings, "check_billing")
    path.write_text('provider = "everyone"\n')

    with pytest.raises(plugins.PluginConfigError, match="provider must be one of"):
        plugins.write_config(settings, "check_billing", {"monthly_budget": "5"})

    assert path.read_text() == 'provider = "everyone"\n'


def test_only_an_installed_plugin_is_changed(settings):
    with pytest.raises(plugins.PluginConfigError, match="check_billing is not installed"):
        plugins.change(settings, "check_billing", {"monthly_budget": "5"})
    assert not plugins.config_path(settings, "check_billing").exists()

    plugins.install(settings, "check_billing")
    plugins.change(settings, "check_billing", {"monthly_budget": "5"})

    assert plugins.read_config(settings, "check_billing")["monthly_budget"] == 5
