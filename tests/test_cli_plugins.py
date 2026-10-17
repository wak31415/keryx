"""`keryx plugins` as the Mac app's Plugins section reads it: each one described, turned on
and off with a JSON answer, and its settings changed in place.

The settings are changed through install's own validation, so no value can add a key or
end its own line; the service may not change them at all.
"""

import json
import tomllib

import pytest
from cli_patch import patch_cli
from typer.testing import CliRunner

from keryx import plugins
from keryx.cli import app
from keryx.config import Settings

runner = CliRunner()
ADDED = {"title", "description", "fields", "needs", "ready", "why"}
KEPT = {"name", "on", "installed", "tool_file", "config_file", "refused", "settings"}


@pytest.fixture
def settings_stub(monkeypatch, tmp_path, every_agent_installed):
    settings = Settings(_env_file=None, openai_api_key="test", data_dir=tmp_path / "keryx")
    patch_cli(monkeypatch, "load_settings", lambda **overrides: settings)
    return settings


def install(*argv):
    result = runner.invoke(app, ["plugins", "install", *argv])
    assert result.exit_code == 0, result.output
    return result


def stored(settings, name):
    return tomllib.loads(plugins.config_path(settings, name).read_text())


def test_the_list_describes_each_plugin_and_keeps_what_it_had(settings_stub):
    result = runner.invoke(app, ["plugins", "list", "--json"])

    assert result.exit_code == 0, result.output
    found = json.loads(result.stdout)["plugins"]
    assert [one["name"] for one in found] == list(plugins.PLUGINS)
    for one in found:
        assert set(one) == KEPT | ADDED
    billing = found[2]
    assert billing["title"] == "Billing" and billing["ready"] is False
    assert billing["needs"] == ["openai-admin", "anthropic-admin"]


def test_an_admin_key_makes_billing_ready(settings_stub, monkeypatch):
    ready = settings_stub.model_copy(update={"openai_admin_key": "sk-admin-shh"})
    patch_cli(monkeypatch, "load_settings", lambda **overrides: ready)

    result = runner.invoke(app, ["plugins", "--json"])

    billing = json.loads(result.stdout)["plugins"][2]
    assert billing["ready"] is True and billing["why"] is None
    assert "shh" not in result.stdout


def test_install_json_says_it_is_on(settings_stub):
    result = install("check_billing", "--set", "monthly_budget=40", "--json")

    assert json.loads(result.stdout) == {
        "name": "check_billing", "on": True,
        "message": "Billing is on from the next call.",
    }


def test_install_json_refused_prints_no_json(settings_stub):
    result = runner.invoke(app, ["plugins", "install", "send_to_slack",
                                 "--set", "channel_id=D123", "--json"])

    assert result.exit_code == 1
    assert result.stdout == "" and "refused" in result.stderr


def test_install_json_of_a_template_says_it_stays_off(settings_stub):
    result = install("cluster_stats", "--template", "--json")

    document = json.loads(result.stdout)
    assert document["name"] == "cluster_stats" and document["on"] is False


def test_install_json_moves_one_plugin_at_a_time(settings_stub):
    result = runner.invoke(app, ["plugins", "install", "--from-settings", "--json"])

    assert result.exit_code == 2 and result.stdout == ""


def test_remove_json_asks_nothing_and_says_it_is_off(settings_stub):
    install("check_billing")

    result = runner.invoke(app, ["plugins", "remove", "check_billing", "--json"])
    again = runner.invoke(app, ["plugins", "remove", "check_billing", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "name": "check_billing", "on": False,
        "message": "Billing is off from the next call; its settings are kept.",
    }
    assert json.loads(again.stdout)["message"] == "Billing was not on."


def test_set_changes_the_file_in_place(settings_stub):
    install("check_billing", "--set", "monthly_budget=40")
    tool = plugins.tool_path(settings_stub, "check_billing")
    tool.write_text(tool.read_text() + "# the owner's own line\n")

    result = runner.invoke(app, ["plugins", "set", "check_billing", "monthly_budget=75",
                                 "provider=anthropic", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "name": "check_billing", "on": True, "message": "Settings saved.",
    }
    assert stored(settings_stub, "check_billing")["monthly_budget"] == 75
    assert stored(settings_stub, "check_billing")["provider"] == "anthropic"
    assert tool.read_text().endswith("# the owner's own line\n")  # not reinstalled


def test_set_text_says_it_is_saved(settings_stub):
    install("check_billing")

    result = runner.invoke(app, ["plugins", "set", "check_billing", "openai_project_id=p1"])

    assert result.exit_code == 0 and "check_billing: settings saved" in result.output


def test_set_takes_the_whole_clusters_table(settings_stub):
    install("cluster_stats", "--cluster", "alpha=gpu")

    result = runner.invoke(app, ["plugins", "set", "cluster_stats", "timeout_s=30",
                                 "--cluster", "beta=pli", "--json"])

    assert result.exit_code == 0, result.output
    assert stored(settings_stub, "cluster_stats")["clusters"] == {"beta": "pli"}
    assert stored(settings_stub, "cluster_stats")["timeout_s"] == 30


def test_set_says_why_a_plugin_that_is_on_does_not_load(settings_stub, monkeypatch):
    plugins.install(settings_stub, "check_email")  # installed, and refused: no Gmail sign-in

    result = runner.invoke(app, ["plugins", "set", "check_email", "effort=high", "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["on"] is False
    assert document["message"] == "Settings saved. Connect Gmail in Integrations first."


@pytest.mark.parametrize(
    ("argv", "says"),
    [(["monthly_budget=lots"], "must be a number"),
     (["provider=everyone"], "must be one of"),
     (['openai_project_id=p1"\nevil = "1'], "bare word"),
     (["openai_project_id=p1 evil=1"], "bare word"),
     (["budget=3"], "has no setting"),
     (["OPENAI_ADMIN_KEY=sk-admin-shh"], "never taken on a command line"),
     (["--cluster", "a=b"], "for cluster_stats")],
)
def test_set_refuses_what_install_refuses_and_writes_nothing(settings_stub, argv, says):
    install("check_billing", "--set", "monthly_budget=40")
    before = plugins.config_path(settings_stub, "check_billing").read_text()

    result = runner.invoke(app, ["plugins", "set", "check_billing", *argv, "--json"])

    assert result.exit_code == 1
    assert says in result.stderr and result.stdout == ""
    assert "shh" not in result.output
    assert plugins.config_path(settings_stub, "check_billing").read_text() == before
    assert "evil" not in stored(settings_stub, "check_billing")


def test_set_refuses_a_guard_that_would_end_its_line(settings_stub):
    install("cluster_stats", "--cluster", "alpha=gpu")

    result = runner.invoke(app, ["plugins", "set", "cluster_stats", 'guard=/x"\nevil = "1'])

    assert result.exit_code == 1 and "one line" in result.stderr
    assert "evil" not in stored(settings_stub, "cluster_stats")


def test_set_of_a_plugin_that_is_not_installed_is_refused(settings_stub):
    result = runner.invoke(app, ["plugins", "set", "check_billing", "monthly_budget=5",
                                 "--json"])

    assert result.exit_code == 1 and result.stdout == ""
    assert "check_billing is not installed" in result.stderr
    assert not plugins.config_path(settings_stub, "check_billing").exists()


def test_set_of_no_plugin_or_nothing_is_refused(settings_stub):
    unknown = runner.invoke(app, ["plugins", "set", "send_fax", "a=b"])
    empty = runner.invoke(app, ["plugins", "set", "check_billing"])

    assert unknown.exit_code == 1 and "no plugin called" in unknown.stderr
    assert empty.exit_code == 2 and "KEY=VALUE" in empty.stderr


@pytest.mark.parametrize(
    "argv",
    [["set", "check_billing", "monthly_budget=5", "--json"],
     ["install", "check_billing", "--json"], ["remove", "check_billing", "--json"]],
)
def test_the_service_may_not_change_a_plugin(settings_stub, monkeypatch, argv):
    install("check_billing", "--set", "monthly_budget=40")
    before = plugins.config_path(settings_stub, "check_billing").read_text()
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = runner.invoke(app, ["plugins", *argv])

    assert result.exit_code == 1 and result.stdout == ""
    assert "only the owner" in result.stderr
    assert plugins.config_path(settings_stub, "check_billing").read_text() == before


def test_set_repairs_what_the_card_says_is_wrong(settings_stub):
    install("check_billing")
    path = plugins.config_path(settings_stub, "check_billing")
    path.write_text('provider = "everyone"\nmonthly_budget = 12\n')

    result = runner.invoke(app, ["plugins", "set", "check_billing", "provider=openai", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["on"] is True
    assert stored(settings_stub, "check_billing")["provider"] == "openai"
    assert stored(settings_stub, "check_billing")["monthly_budget"] == 12
