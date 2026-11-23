"""`send_to_slack` became `slack_report`: what an owner installed keeps working, with
nothing asked of them, and an older build that shares the directory still finds its own."""

import tomllib

import pytest

from keryx import plugins
from keryx.plugins import slack as plugin
from plugins.helpers import call, loaded, names, offered, refusal

OLD, NEW = "send_to_slack", "slack_report"
#: The released one line of `send_to_slack.py`, as it sits on a machine installed before.
RELEASED = '''"""Keryx's Slack plugin, turned on with `keryx plugins install send_to_slack`.

Its settings are in send_to_slack.toml beside this file. The code is `keryx.plugins.slack`, so
an update to Keryx reaches this copy; `keryx plugins remove send_to_slack` turns it off.
"""

from pathlib import Path

from keryx.plugins.slack import send_to_slack_tool

send_to_slack = send_to_slack_tool(Path(__file__).with_suffix(".toml"))
'''


@pytest.fixture
def installed_before(settings):
    """A machine that turned the plugin on under its old name, and has its bot token."""
    settings = settings.model_copy(update={"slack_bot_token": "xoxb-1"})
    tools = settings.custom_tools_dir
    tools.mkdir(parents=True, exist_ok=True)
    (tools / f"{OLD}.py").write_text(RELEASED)
    (tools / f"{OLD}.toml").write_text('channel_id = "U0OWNER"\nmcp_server = ""\n')
    for path in tools.iterdir():
        path.chmod(0o600)
    return settings


def stems(settings) -> set[str]:
    return {path.name for path in settings.custom_tools_dir.iterdir()
            if not path.name.startswith("__")}


def test_the_shipped_copy_of_the_old_one_line_is_the_released_one():
    """It is what an older build loads, so it must go on calling a name that build has."""
    assert plugins._template(f"{OLD}.py") == RELEASED


def test_an_installed_pair_keeps_loading_and_offers_the_tool_under_its_new_name(
    installed_before,
):
    assert names(installed_before) == {NEW}
    assert refusal(installed_before, OLD) is None
    assert stems(installed_before) == {f"{OLD}.py", f"{OLD}.toml"}, "and nothing was moved"


async def test_the_tool_it_offers_sends_as_it_always_did(installed_before, monkeypatch):
    sent = []

    class Sender:
        async def send(self, text, *, fallback=None):
            sent.append(text)
            return True

    path = plugins.config_path(installed_before, NEW)
    from plugins.helpers import loading

    with loading(installed_before):
        tool = plugin.send_to_slack_tool(path, sender=Sender())

    assert path.name == f"{OLD}.toml" and tool.name == NEW
    assert await call(offered(tool, installed_before), NEW, {"message": "the diff"}) == {
        "status": "sent"}
    assert sent == ["the diff"]


def test_everything_that_reads_its_settings_reads_the_old_file(installed_before):
    assert plugins.read_config(installed_before, NEW)["channel_id"] == "U0OWNER"
    assert plugins.is_on(installed_before, NEW)
    assert plugin.slack_route(installed_before).channel == "U0OWNER"
    [workspace] = plugin.connected(installed_before)
    assert (workspace.name, workspace.channel) == ("default", "U0OWNER")


def test_a_write_goes_to_the_file_there_is_and_never_makes_a_second(installed_before):
    path = plugins.write_config(installed_before, NEW, {"owner_id": "U0OWNER"})

    assert path.name == f"{OLD}.toml"
    assert stems(installed_before) == {f"{OLD}.py", f"{OLD}.toml"}
    assert tomllib.loads(path.read_text())["owner_id"] == "U0OWNER"


def test_keryx_plugins_shows_one_plugin_not_two(installed_before):
    listed = [status for status in plugins.status(installed_before)
              if status.name in (OLD, NEW)]

    assert [(status.name, status.on, status.tool_file.name) for status in listed] == [
        (NEW, True, f"{OLD}.py")]
    assert OLD not in plugins.PLUGINS and plugins.plugin(OLD).name == NEW


def test_turning_it_off_and_on_again_stays_under_the_name_its_files_have(installed_before):
    assert plugins.remove(installed_before, NEW)
    assert stems(installed_before) == {f"{OLD}.toml.off"}

    plugins.install(installed_before, NEW)

    assert stems(installed_before) == {f"{OLD}.py", f"{OLD}.toml"}
    assert (installed_before.custom_tools_dir / f"{OLD}.py").read_text() == RELEASED
    assert names(installed_before) == {NEW}


def test_an_older_build_still_finds_its_plugin_after_this_build_has_run(installed_before):
    """Several builds run against one tools directory here. Whatever this one does short
    of the owner's explicit install, the pair the older one knows is still there, its one
    line unchanged, and its settings still the keys that build reads."""
    from keryx.setup import slack as setup

    plugins.write_config(installed_before, NEW, {"access": "converse", "owner_id": "U0OWNER",
                                                 "team_id": "T0ACME", "bot_user_id": "U0KERYX"})
    plugins.install(installed_before, NEW)
    setup._write_workspace(installed_before, plugins.read_config(installed_before, NEW),
                           "work", {"channel_id": "U0WORK", "access": "announce"})
    plugins.status(installed_before)
    loaded(installed_before)

    tools = installed_before.custom_tools_dir
    assert stems(installed_before) == {f"{OLD}.py", f"{OLD}.toml"}
    assert (tools / f"{OLD}.py").read_text() == RELEASED
    kept = tomllib.loads((tools / f"{OLD}.toml").read_text())
    assert (kept["channel_id"], kept["mcp_server"]) == ("U0OWNER", "")
    assert kept["workspace-work"]["channel_id"] == "U0WORK"


def test_only_the_owners_explicit_install_renames_the_pair_in_place(installed_before):
    plugins.write_config(installed_before, NEW, {"owner_id": "U0OWNER"})

    plugins.install(installed_before, NEW, rename=True)

    assert stems(installed_before) == {f"{NEW}.py", f"{NEW}.toml"}
    assert (installed_before.custom_tools_dir / f"{NEW}.py").read_text() == (
        plugins._template(f"{NEW}.py"))
    kept = plugins.read_config(installed_before, NEW)
    assert (kept["channel_id"], kept["owner_id"]) == ("U0OWNER", "U0OWNER")
    assert names(installed_before) == {NEW}
    assert not plugins.adopt(installed_before, NEW), "once: there is nothing left to rename"


def test_a_one_line_file_the_owner_edited_is_moved_as_it_is_and_still_loads(installed_before):
    edited = RELEASED + "\n# mine\n"
    (installed_before.custom_tools_dir / f"{OLD}.py").write_text(edited)

    plugins.adopt(installed_before, NEW)

    assert (installed_before.custom_tools_dir / f"{NEW}.py").read_text() == edited
    assert names(installed_before) == {NEW} and refusal(installed_before, NEW) is None


def test_settings_set_aside_and_a_draft_are_renamed_with_the_rest(settings):
    tools = settings.custom_tools_dir
    tools.mkdir(parents=True, exist_ok=True)
    (tools / f"{OLD}.toml.off").write_text('channel_id = "U0OWNER"\n')
    (tools / f"_{OLD}.py").write_text(RELEASED)

    assert plugins.adopt(settings, NEW)

    assert stems(settings) == {f"{NEW}.toml.off", f"_{NEW}.py"}
    assert plugins.read_config(settings, NEW)["channel_id"] == "U0OWNER"


def test_a_machine_that_never_had_the_old_name_only_ever_gets_the_new(settings):
    settings = settings.model_copy(update={"slack_bot_token": "xoxb-1"})
    plugins.write_config(settings, NEW, {"channel_id": "U0OWNER"})
    plugins.install(settings, NEW)

    assert stems(settings) == {f"{NEW}.py", f"{NEW}.toml"}
    assert not plugins.adopt(settings, NEW)
    assert plugins.files_name(settings, OLD) == NEW


def test_once_a_file_of_the_new_name_exists_the_old_ones_are_not_read(installed_before):
    """Never both: a second copy would be two places for one setting."""
    (installed_before.custom_tools_dir / f"{NEW}.toml").write_text('channel_id = "C0NEW"\n')

    assert plugins.files_name(installed_before, NEW) == NEW
    assert plugins.read_config(installed_before, NEW)["channel_id"] == "C0NEW"


def test_the_old_name_is_still_taken_on_a_command_line_and_means_the_new(settings):
    assert plugins.canonical(OLD) == NEW and plugins.canonical("check_email") == "check_email"
    assert plugins.command_line_values(OLD, ["channel_id=C0X"]) == {"channel_id": "C0X"}
    assert plugins.render_config(OLD, {}).startswith("# slack_report:")


def test_with_both_pairs_there_the_new_one_is_loaded_and_the_old_one_stands_aside(
    installed_before,
):
    """An older build made its pair after this one made its own. Two files would define
    one tool and the older would load first: it refuses itself instead, so one tool is
    offered, from the settings that are written, and `keryx plugins` shows one state."""
    tools = installed_before.custom_tools_dir
    (tools / f"{NEW}.py").write_text(plugins._template(f"{NEW}.py"))
    (tools / f"{NEW}.toml").write_text('channel_id = "C0NEW"\n')
    for path in tools.iterdir():
        path.chmod(0o600)

    assert names(installed_before) == {NEW} and refusal(installed_before, NEW) is None
    assert "is the one that is loaded" in refusal(installed_before, OLD)
    assert "can be deleted" in refusal(installed_before, OLD)
    assert plugin.slack_route(installed_before).channel == "C0NEW"
    [status] = [each for each in plugins.status(installed_before) if each.name == NEW]
    assert (status.on, status.refused, status.tool_file.name) == (True, None, f"{NEW}.py")
