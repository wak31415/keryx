"""The accounts Keryx connects to, as the Mac app's Integrations section shows them.

Each is connected or not, and when not, says why in the app's words — never a command,
because the app has a button for each — with nothing on the network but `gh`, faked here.
"""

import json

import pytest

from keryx.integrations import accounts
from keryx.integrations.gmail import token_path
from keryx.issues import SKILL, GhStatus

IDS = ["google", "gmail", "slack", "github", "openai-admin", "anthropic-admin"]
KEYS = {"id", "title", "description", "connected", "why", "secrets", "settings", "signin",
        "used_by"}


def gh_never():
    raise AssertionError("gh was asked while issue reports are off")


def rows(settings, gh=gh_never, **kwargs):
    return {row["id"]: row for row in accounts.integrations(settings, gh_status=gh, **kwargs)}


@pytest.fixture
def checkout(tmp_path):
    """A Keryx clone with the report skill in it."""
    root = tmp_path / "checkout"
    (root / SKILL).parent.mkdir(parents=True)
    (root / SKILL).write_text("# skill\n")
    return root


def with_client(settings):
    return settings.model_copy(update={"google_oauth_client_id": "id.apps",
                                       "google_oauth_client_secret": "client-shh"})


def test_a_fresh_machine_has_all_six_and_none_is_connected(settings):
    found = accounts.integrations(settings, gh_status=gh_never)

    assert [row["id"] for row in found] == IDS
    for row in found:
        assert set(row) == KEYS
        assert row["connected"] is False
        assert row["why"], row["id"]
        # The app has a button for each: a command in a card is one it cannot run.
        assert "`" not in row["why"] and "keryx " not in row["why"], row["why"]
        assert all(secret["set"] is False for secret in row["secrets"])


def test_each_says_what_it_edits_how_it_signs_in_and_which_plugins_use_it(settings):
    found = rows(settings)

    assert {name: row["used_by"] for name, row in found.items()} == {
        "google": [], "gmail": ["check_email"], "slack": ["send_to_slack"], "github": [],
        "openai-admin": ["check_billing"], "anthropic-admin": ["check_billing"],
    }
    assert {name: row["signin"] for name, row in found.items()} == {
        "google": "google-workspace", "gmail": "gmail", "slack": None, "github": "gh",
        "openai-admin": None, "anthropic-admin": None,
    }
    assert found["slack"]["secrets"] == [
        {"key": "SLACK_BOT_TOKEN", "title": "Slack bot token", "set": False}
    ]
    assert [s["key"] for s in found["openai-admin"]["secrets"]] == ["OPENAI_ADMIN_KEY"]
    assert [s["key"] for s in found["anthropic-admin"]["secrets"]] == ["ANTHROPIC_ADMIN_KEY"]
    assert found["github"]["settings"] == ["ISSUE_REPORTING", "ISSUE_REPO"]
    assert "GOOGLE_WORKSPACE_MCP" in found["google"]["settings"]
    # The OAuth client is configuration the two Google cards share: neither disconnects it.
    for name in ("google", "gmail"):
        assert "GOOGLE_CLIENT_SECRETS_FILE" not in found[name]["settings"]


def test_a_secret_set_connects_its_card_and_its_value_is_never_shown(settings):
    settings = settings.model_copy(update={"slack_bot_token": "xoxb-shh",
                                           "anthropic_admin_key": "sk-ant-admin-shh"})

    found = rows(settings)

    assert found["slack"]["connected"] is True and found["slack"]["why"] is None
    assert found["slack"]["secrets"][0]["set"] is True
    assert found["anthropic-admin"]["connected"] is True
    assert found["openai-admin"]["connected"] is False
    assert "shh" not in json.dumps(list(found.values()))


def test_gmail_is_connected_by_its_sign_in(settings):
    path = token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    assert "OAuth client" in rows(settings)["gmail"]["why"]
    assert rows(with_client(settings))["gmail"]["why"] == "Not signed in."
    path.write_text('{"refresh_token": "r"}')
    assert rows(settings)["gmail"]["connected"] is True
    assert rows(settings)["gmail"]["why"] is None


def test_google_for_agents_needs_the_server_a_client_and_a_sign_in(settings):
    off = rows(with_client(settings))["google"]
    assert off["connected"] is False and "Claude" in off["why"]

    on = with_client(settings).model_copy(update={"google_workspace_mcp": True})
    assert rows(on)["google"]["why"] == "Not signed in."
    no_client = settings.model_copy(update={"google_workspace_mcp": True})
    assert "OAuth client" in rows(no_client)["google"]["why"]

    credentials = settings.data_dir / "google"
    credentials.mkdir(parents=True)
    (credentials / "ada@example.com.json").write_text("{}")
    assert rows(on)["google"]["connected"] is True
    assert rows(on)["google"]["why"] is None


def test_codex_without_google_is_told_what_it_lacks(settings):
    settings = settings.model_copy(update={"agents_enabled": ["claude", "codex"]})

    assert "Codex" in rows(settings)["google"]["why"]


def test_issue_reports_off_never_asks_gh(settings, checkout):
    found = rows(settings.model_copy(update={"keryx_checkout": checkout}))["github"]

    assert found["connected"] is False and found["why"] == "Issue reports are off."


@pytest.mark.parametrize(
    ("gh", "connected", "why"),
    [(GhStatus(installed=True, signed_in=True, account="ada"), True, None),
     (GhStatus(installed=False), False, "gh"),
     (GhStatus(installed=True, signed_in=False), False, "signed in")],
)
def test_issue_reports_on_are_as_ready_as_gh(settings, checkout, gh, connected, why):
    settings = settings.model_copy(update={"issue_reporting": True, "keryx_checkout": checkout})

    found = rows(settings, gh=lambda: gh)["github"]

    assert found["connected"] is connected
    assert (found["why"] is None) if why is None else (why in found["why"])


def test_issue_reports_on_without_the_skill_say_so_before_gh(settings, tmp_path):
    settings = settings.model_copy(update={"issue_reporting": True,
                                           "keryx_checkout": tmp_path / "nothing"})

    assert "checkout" in rows(settings)["github"]["why"]


def test_only_the_ids_asked_for_are_looked_at(settings):
    settings = settings.model_copy(update={"issue_reporting": True, "slack_bot_token": "x"})

    assert accounts.connected(settings, ["slack", "gmail"], gh_status=gh_never) == {
        "slack": True, "gmail": False,
    }


def test_an_unknown_id_is_refused(settings):
    with pytest.raises(KeyError):
        accounts.connected(settings, ["fax"])
