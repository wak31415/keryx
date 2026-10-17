"""`keryx integrations` and `keryx auth logout`: the Mac app's Integrations section.

The first only reads; the second removes the files one Google sign-in left, and is the
owner's alone. Nothing here reaches the network: `gh` is faked.
"""

import json

import pytest
from cli_patch import patch_cli
from typer.testing import CliRunner

from keryx.cli import app
from keryx.config import Settings
from keryx.integrations.gmail import token_path
from keryx.issues import GhStatus
from keryx.setup import google

runner = CliRunner()


@pytest.fixture
def settings_stub(monkeypatch, tmp_path, every_agent_installed):
    settings = Settings(_env_file=None, openai_api_key="test", data_dir=tmp_path / "keryx",
                        google_client_secrets_file=tmp_path / "no-client.json")
    patch_cli(monkeypatch, "load_settings", lambda **overrides: settings)
    monkeypatch.setattr("keryx.issues.gh_status",
                        lambda: GhStatus(installed=True, signed_in=True, account="ada"))
    return settings


def signed_in(settings):
    """A Gmail token and Google-for-agents credentials, beside the client file."""
    client = settings.config_dir / google.GOOGLE_CLIENT_FILE
    token = token_path(settings)
    stored = google.credentials_dir(settings) / "ada@example.com.json"
    for path in (client, token, stored):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    return client, token, stored


def test_integrations_json_lists_every_account(settings_stub):
    result = runner.invoke(app, ["integrations", "--json"])

    assert result.exit_code == 0, result.output
    found = json.loads(result.stdout)["integrations"]
    assert [row["id"] for row in found] == [
        "google", "gmail", "slack", "github", "openai-admin", "anthropic-admin",
    ]
    assert all(row["connected"] is False for row in found)


def test_integrations_text_says_each_and_why(settings_stub):
    signed_in(settings_stub)

    result = runner.invoke(app, ["integrations"])

    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line[:1] != " "}
    assert "  connected" in lines["gmail"] and "not connected" in lines["slack"]
    assert "Issue reports are off." in result.output


@pytest.mark.parametrize("name", ["gmail", "google-workspace"])
def test_logout_json_says_signed_out(settings_stub, name):
    signed_in(settings_stub)

    result = runner.invoke(app, ["auth", "logout", name, "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["name"] == name and document["signed_in"] is False
    assert document["message"]


def test_logout_gmail_removes_its_token_and_keeps_the_client(settings_stub):
    client, token, stored = signed_in(settings_stub)

    result = runner.invoke(app, ["auth", "logout", "gmail"])

    assert result.exit_code == 0, result.output
    assert "Gmail" in result.output
    assert not token.exists() and client.exists() and stored.exists()


def test_logout_google_workspace_removes_its_credentials_and_keeps_the_client(settings_stub):
    client, token, stored = signed_in(settings_stub)

    result = runner.invoke(app, ["auth", "logout", "google-workspace", "--json"])

    assert result.exit_code == 0, result.output
    assert not stored.exists() and client.exists() and token.exists()
    assert "removed" in json.loads(result.stdout)["message"]


def test_logout_when_nothing_was_signed_in_says_so(settings_stub):
    result = runner.invoke(app, ["auth", "logout", "gmail", "--json"])

    assert result.exit_code == 0, result.output
    assert "not signed in" in json.loads(result.stdout)["message"]


def test_the_service_may_not_sign_out(settings_stub, monkeypatch):
    _, token, _ = signed_in(settings_stub)
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = runner.invoke(app, ["auth", "logout", "gmail", "--json"])

    assert result.exit_code == 1
    assert "only the owner" in result.stderr and result.stdout == ""
    assert token.exists()


def test_logout_of_anything_else_is_refused(settings_stub):
    result = runner.invoke(app, ["auth", "logout", "claude", "--json"])

    assert result.exit_code == 2
    assert "gmail" in result.stderr and result.stdout == ""


def test_the_service_is_refused_before_the_name_is_looked_at(settings_stub, monkeypatch):
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = runner.invoke(app, ["auth", "logout", "claude", "--json"])

    assert result.exit_code == 1 and "only the owner" in result.stderr
