"""The Google account: the scope table, whose client, the one token, and its copy for the
agents' MCP server. No network: nothing here calls Google."""

import json
import stat
from pathlib import Path

import pytest

from keryx.integrations import google
from keryx.integrations.google import (
    CALENDAR_EVENTS,
    CALENDAR_FULL,
    CALENDAR_READONLY,
    DRIVE_FILE,
    DRIVE_FULL,
    DRIVE_READONLY,
    GMAIL_MODIFY,
    GMAIL_READONLY,
    GMAIL_SETTINGS,
    SERVICES,
    GoogleToken,
)

ALL = tuple(SERVICES)
AUTH = "https://www.googleapis.com/auth/"
#: Read at import, before the suite's fixture points every test away from it.
PACKAGED_CLIENT = google.PUBLISHED_CLIENT_FILE


def token(services=ALL, **changes) -> GoogleToken:
    values = {
        "refresh_token": "1//refresh-token",
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "GOCSPX-client-secret",
        "scopes": google.requested_scopes(services),
        "email": "ada@example.com",
    }
    return GoogleToken(**{**values, **changes})


# --- the scope table ----------------------------------------------------------------------


def test_the_table_requests_exactly_the_scopes_that_were_decided():
    """Every scope here has to be justified to Google: adding one is a decision, not a diff."""
    assert {name: service.scopes for name, service in SERVICES.items()} == {
        "gmail": (AUTH + "gmail.modify",),
        "calendar": (AUTH + "calendar.readonly", AUTH + "calendar.events"),
        "drive": (AUTH + "drive.readonly", AUTH + "drive.file"),
        "docs": (AUTH + "documents",),
        "sheets": (AUTH + "spreadsheets",),
        "slides": (AUTH + "presentations",),
    }


def test_a_sign_in_always_asks_who_it_is():
    assert google.requested_scopes(["calendar"]) == (
        "openid",
        AUTH + "userinfo.email",
        CALENDAR_READONLY,
        CALENDAR_EVENTS,
    )


@pytest.mark.parametrize("service", ["docs", "sheets", "slides"])
def test_a_document_service_brings_drive_with_it(service):
    scopes = google.requested_scopes([service])

    assert DRIVE_READONLY in scopes and DRIVE_FILE in scopes
    assert google.connected_services(scopes) == ("drive", service)


def test_everything_asks_for_no_scope_twice_and_nothing_broad():
    scopes = google.requested_scopes(ALL)

    assert len(scopes) == len(set(scopes)) == 10
    assert not {DRIVE_FULL, CALENDAR_FULL, GMAIL_SETTINGS, GMAIL_READONLY} & set(scopes)


def test_a_service_is_connected_only_when_every_scope_it_needs_was_granted():
    granted = [s for s in google.requested_scopes(ALL) if s not in (CALENDAR_EVENTS, DRIVE_FILE)]

    assert google.connected_services(granted) == ("gmail",)


def test_a_broader_scope_an_older_sign_in_holds_stands_in_for_the_narrow_ones():
    assert google.connected_services([GMAIL_MODIFY, CALENDAR_FULL, DRIVE_FULL]) == (
        "gmail",
        "calendar",
        "drive",
    )


@pytest.mark.parametrize(
    ("scopes", "reads"),
    [([GMAIL_MODIFY], True), ([GMAIL_READONLY], True), ([CALENDAR_READONLY], False), (None, False)],
)
def test_gmail_is_read_with_this_sign_ins_scope_or_an_older_ones(scopes, reads):
    assert google.reads_gmail(scopes) is reads


def test_a_read_only_gmail_token_is_not_gmail_connected_for_the_agents():
    assert google.connected_services([GMAIL_READONLY]) == ()


def test_services_are_named_in_a_sentence():
    assert google.labels(["gmail"]) == "Gmail"
    assert google.labels(["gmail", "docs"]) == "Gmail and Docs"
    assert google.labels(ALL) == "Gmail, Calendar, Drive, Docs, Sheets and Slides"


def test_services_on_the_command_line_are_checked_and_put_in_order():
    assert google.parse_services("slides, Gmail") == ("gmail", "slides")
    with pytest.raises(ValueError, match="chat is not a Google service; choose from: gmail"):
        google.parse_services("gmail,chat")
    with pytest.raises(ValueError, match="nothing is not"):
        google.parse_services(" , ")


# --- which client, and whose --------------------------------------------------------------


def published_file(tmp_path, body) -> object:
    path = tmp_path / "google_client.json"
    path.write_text(json.dumps(body))
    return path


INSTALLED = {"installed": {"client_id": "keryx.apps", "client_secret": "GOCSPX-published"}}


def test_the_published_client_is_a_data_file_inside_the_package():
    """The wheel is built from `src/keryx`, so a file here ships with no build change."""
    import keryx

    assert PACKAGED_CLIENT == Path(keryx.__file__).resolve().parent / "data" / "google_client.json"


def test_with_no_client_anywhere_there_is_none(settings):
    assert google.resolve_client(settings) is None


def test_the_published_client_is_used_when_the_owner_has_none(settings, tmp_path):
    client = google.resolve_client(settings, published=published_file(tmp_path, INSTALLED))

    assert (client.client_id, client.client_secret) == ("keryx.apps", "GOCSPX-published")
    assert client.published and client.whose == "Keryx's Google app"
    assert client.services == ALL and client.withheld == ()


def test_the_owners_own_client_wins_and_offers_everything(settings, tmp_path):
    settings.google_oauth_client_id = "mine"
    settings.google_oauth_client_secret = "my-secret"
    limited = {**INSTALLED, "keryx_services": ["calendar"]}

    client = google.resolve_client(settings, published=published_file(tmp_path, limited))

    assert (client.client_id, client.published) == ("mine", False)
    assert client.whose == "your own Google client" and client.services == ALL


def test_the_packaged_file_is_where_the_published_client_is_looked_for(
    settings, tmp_path, monkeypatch
):
    monkeypatch.setattr(google, "PUBLISHED_CLIENT_FILE", published_file(tmp_path, INSTALLED))

    assert google.resolve_client(settings).published


def test_a_published_client_offers_only_the_services_it_lists(tmp_path):
    body = {**INSTALLED, "keryx_services": ["calendar", "drive", "docs", "tasks"]}

    client = google.published_client(published_file(tmp_path, body))

    assert client.services == ("calendar", "drive", "docs")
    assert client.withheld == ("gmail", "sheets", "slides")


def test_a_published_client_without_drive_cannot_offer_what_builds_on_it(tmp_path):
    body = {**INSTALLED, "keryx_services": ["calendar", "docs", "sheets"]}

    assert google.published_client(published_file(tmp_path, body)).services == ("calendar",)


def test_a_client_file_with_no_secret_is_a_client_with_an_empty_one(tmp_path):
    client = google.published_client(published_file(tmp_path, {"installed": {"client_id": "pub"}}))

    assert (client.client_id, client.client_secret) == ("pub", "")


def test_a_packaged_file_that_is_not_a_client_is_no_client(tmp_path, caplog):
    assert google.published_client(published_file(tmp_path, {"installed": {}})) is None
    assert "unusable" in caplog.text


# --- the token ----------------------------------------------------------------------------


def test_the_token_is_written_private_and_read_back(settings):
    path = google.write_token(settings, token())

    assert path == settings.data_dir / "google_token.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert google.read_token(settings) == token()
    assert set(json.loads(path.read_text())) == {
        "refresh_token", "client_id", "client_secret", "token_uri", "scopes", "email"
    }  # fmt: skip


@pytest.mark.parametrize("text", ["", "[]", "{}", '{"refresh_token": "r"}', "not json"])
def test_a_token_file_that_cannot_refresh_is_no_token(settings, text):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    google.token_path(settings).write_text(text)

    assert google.read_token(settings) is None


def test_no_token_file_is_no_token(settings):
    assert google.read_token(settings) is None


# --- the copy the agents' MCP server reads ------------------------------------------------


def test_the_export_is_the_file_workspace_mcp_reads(settings):
    path = google.export_for_mcp(settings, token(email="ada+keryx@example.com"))

    assert path == settings.data_dir / "google" / "ada%2Bkeryx@example.com.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {
        "token": None,
        "refresh_token": "1//refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "GOCSPX-client-secret",
        "scopes": list(google.requested_scopes(ALL)),
        "expiry": None,
    }


def test_the_servers_own_refreshed_token_is_left_alone(settings):
    path = google.export_for_mcp(settings, token())
    refreshed = {**json.loads(path.read_text()), "token": "ya29.fresh", "expiry": "2026-11-22"}
    path.write_text(json.dumps(refreshed))

    google.export_for_mcp(settings, token())

    assert json.loads(path.read_text())["token"] == "ya29.fresh"


@pytest.mark.parametrize(
    "change",
    [
        {"refresh_token": "1//another"},
        {"client_id": "other.apps"},
        {"client_secret": "GOCSPX-other"},
        {"scopes": google.requested_scopes(["gmail"])},
    ],
)
def test_a_new_sign_in_replaces_the_export(settings, change):
    path = google.export_for_mcp(settings, token())
    path.write_text(json.dumps({**json.loads(path.read_text()), "token": "ya29.stale"}))

    google.export_for_mcp(settings, token(**change))

    stored = json.loads(path.read_text())
    assert stored["token"] is None
    assert all(stored[key] == list(value) if key == "scopes" else stored[key] == value
               for key, value in change.items())


def test_an_export_that_cannot_be_read_is_written_again(settings):
    path = google.export_for_mcp(settings, token())
    path.write_text("{half a file")

    google.export_for_mcp(settings, token())

    assert json.loads(path.read_text())["refresh_token"] == "1//refresh-token"


def test_a_new_sign_in_leaves_no_other_accounts_credential_behind(settings):
    """The server's tools take an address as an argument: a file is an account to act as."""
    before = google.export_for_mcp(settings, token(email="old@example.com"))
    (before.parent / "oauth_states.json").write_text("{}")  # the server's own, not an account

    path = google.export_for_mcp(settings, token(email="new@example.com"), only=True)

    assert not before.exists()
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "new@example.com.json", "oauth_states.json"
    ]  # fmt: skip


def test_an_ordinary_export_touches_nobody_elses_file(settings):
    before = google.export_for_mcp(settings, token(email="old@example.com"))

    google.export_for_mcp(settings, token(email="new@example.com"))

    assert before.exists()


def test_a_token_with_no_address_cannot_be_exported(settings):
    assert google.export_for_mcp(settings, token(email="")) is None
    assert not google.mcp_credentials_dir(settings).exists()


# --- the server a subagent is given -------------------------------------------------------


def signed_in(settings, services=ALL, **changes) -> GoogleToken:
    settings.google_workspace_mcp = True
    made = token(services, **changes)
    google.write_token(settings, made)
    return made


def test_off_means_no_server_even_when_signed_in(settings):
    signed_in(settings)
    settings.google_workspace_mcp = False

    assert google.mcp_server(settings) is None


def test_on_and_not_signed_in_means_no_server(settings):
    settings.google_workspace_mcp = True

    assert google.mcp_server(settings) is None


def test_the_server_is_pinned_and_started_on_what_is_connected_with_no_client(settings):
    signed_in(settings)

    server = google.mcp_server(settings)

    assert server.config == {
        "type": "stdio",
        # Started through `env -u`, which takes an OAuth client out of the server's
        # environment whatever its parent's holds, and whatever a client does with an
        # empty value in a server's `env`.
        "command": "env",
        "args": [
            "-u", "GOOGLE_OAUTH_CLIENT_ID", "-u", "GOOGLE_OAUTH_CLIENT_SECRET",
            "-u", "GOOGLE_CLIENT_SECRET_PATH", "-u", "GOOGLE_CLIENT_SECRETS",
            "uvx", "--from", "workspace-mcp==2.6.0", "workspace-mcp",
            "--tools", "gmail", "calendar", "drive", "docs", "sheets", "slides",
            "--transport", "stdio",
            "--single-user",
            "--disabled-tools", "start_google_auth",
            "list_gmail_filters", "manage_gmail_filter", "manage_calendar",
            "manage_document_comment", "manage_spreadsheet_comment",
            "manage_presentation_comment",
        ],
        "env": {
            "WORKSPACE_MCP_CREDENTIALS_DIR": str(settings.data_dir / "google"),
            "USER_GOOGLE_EMAIL": "ada@example.com",
        },
    }  # fmt: skip
    assert server.services == ALL and not server.older
    assert (settings.data_dir / "google" / "ada@example.com.json").is_file()


def test_only_the_connected_services_and_their_tools_are_named(settings):
    signed_in(settings, ["calendar"])

    args = google.mcp_server(settings).config["args"]

    assert args[args.index("--tools") + 1 : args.index("--transport")] == ["calendar"]
    assert args[args.index("--disabled-tools") + 1 :] == ["start_google_auth", "manage_calendar"]


def test_the_servers_credentials_are_what_gets_redacted(settings):
    signed_in(settings)
    path = google.export_for_mcp(settings, token())
    path.write_text(json.dumps({**json.loads(path.read_text()), "token": "ya29.access-token"}))

    server = google.mcp_server(settings)

    assert server.secrets == ("1//refresh-token", "GOCSPX-client-secret", "ya29.access-token")
    assert not any(secret in json.dumps(server.config) for secret in server.secrets)


def test_the_server_is_never_handed_an_oauth_client_even_from_keryxs_environment(settings):
    """Every variable that release reads a client from is unset on the way in, in the
    command itself: nothing here relies on how an agent's CLI passes an empty value on."""
    signed_in(settings)

    config = google.mcp_server(settings).config

    unset = config["args"][: config["args"].index("uvx")]
    assert config["command"] == "env"
    assert unset == [part for name in google.MCP_UNSET_ENV for part in ("-u", name)]
    assert set(google.MCP_UNSET_ENV) == {
        "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET_PATH",
        "GOOGLE_CLIENT_SECRETS",
    }  # fmt: skip
    assert not set(config["env"]) & set(google.MCP_UNSET_ENV)


def test_a_client_with_no_secret_gets_no_server(settings):
    """The server's library would send an empty secret on every refresh."""
    signed_in(settings, client_secret="")

    assert google.mcp_server(settings) is None
    assert not google.mcp_credentials_dir(settings).exists()


def test_signed_in_with_nothing_connected_means_no_server(settings):
    signed_in(settings, scopes=("openid",))

    assert google.mcp_server(settings) is None


# --- an install that signed its agents in the old way -------------------------------------

#: What workspace-mcp's own consent asked for with `--tools gmail calendar`.
OLDER_SCOPES = [
    AUTH + "userinfo.email", AUTH + "userinfo.profile", "openid",
    GMAIL_READONLY, AUTH + "gmail.send", AUTH + "gmail.compose", GMAIL_MODIFY,
    AUTH + "gmail.labels", GMAIL_SETTINGS,
    CALENDAR_FULL, CALENDAR_READONLY, CALENDAR_EVENTS,
]  # fmt: skip


def older_install(settings, email="ada@example.com", **changes):
    settings.google_workspace_mcp = True
    directory = settings.data_dir / "google"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{email}.json"
    body = {
        "token": "ya29.old", "refresh_token": "1//old-refresh", "token_uri": google.TOKEN_URL,
        "client_id": "mine.apps", "client_secret": "GOCSPX-mine", "scopes": OLDER_SCOPES,
        "expiry": "2026-01-01T00:00:00",
    }  # fmt: skip
    path.write_text(json.dumps({**body, **changes}))
    return path


def test_an_older_install_keeps_its_tools_and_its_file_untouched(settings):
    path = older_install(settings)
    before = path.read_text()

    server = google.mcp_server(settings)

    args = server.config["args"]
    assert server.older and server.services == ("gmail", "calendar")
    assert args[args.index("--tools") + 1 : args.index("--transport")] == ["gmail", "calendar"]
    # Its scopes cover filters and whole calendars, so those tools stay.
    assert args[args.index("--disabled-tools") + 1 :] == ["start_google_auth"]
    assert server.config["env"] == {
        "WORKSPACE_MCP_CREDENTIALS_DIR": str(path.parent),
        "USER_GOOGLE_EMAIL": "ada@example.com",
    }
    assert server.secrets == ("1//old-refresh", "GOCSPX-mine", "ya29.old")
    assert path.read_text() == before and google.read_token(settings) is None


def test_an_older_install_with_several_accounts_uses_the_one_that_is_set(settings):
    older_install(settings, "ada@example.com")
    older_install(settings, "bob@example.com")
    assert google.mcp_server(settings) is None  # nothing says which

    settings.user_google_email = "Bob@example.com"

    assert google.mcp_server(settings).config["env"]["USER_GOOGLE_EMAIL"] == "bob@example.com"


def test_the_servers_own_bookkeeping_file_is_not_a_second_account(settings):
    """workspace-mcp leaves `oauth_states.json` beside its credentials for ever."""
    older_install(settings)
    (settings.data_dir / "google" / "oauth_states.json").write_text("{}")

    server = google.mcp_server(settings)

    assert server.older and server.config["env"]["USER_GOOGLE_EMAIL"] == "ada@example.com"


def test_an_older_credential_under_its_sanitised_name_is_found(settings):
    """Before 2.x the server named the file with every odd character turned into `_`."""
    older_install(settings, "ada_keryx@example.com")
    settings.user_google_email = "ada+keryx@example.com"

    server = google.mcp_server(settings)

    assert server.older
    # The server finds that file by the address, so the address is what it is handed.
    assert server.config["env"]["USER_GOOGLE_EMAIL"] == "ada+keryx@example.com"


def test_a_saved_address_that_matches_nothing_uses_the_only_credential_there_is(settings):
    """An alias: the account signed in as ada@, and the owner wrote their other address."""
    older_install(settings, "ada@example.com")
    settings.user_google_email = "a.lovelace@example.org"

    server = google.mcp_server(settings)

    assert server.older and server.config["env"]["USER_GOOGLE_EMAIL"] == "ada@example.com"


def test_a_saved_address_that_matches_nothing_cannot_choose_between_two(settings):
    older_install(settings, "ada@example.com")
    older_install(settings, "bob@example.com")
    settings.user_google_email = "carol@example.com"

    assert google.mcp_server(settings) is None


def test_an_older_credential_that_cannot_refresh_is_not_used(settings):
    older_install(settings, refresh_token=None)

    assert google.mcp_server(settings) is None


def test_keryxs_own_token_wins_over_an_older_credential(settings):
    older_install(settings, "bob@example.com")
    signed_in(settings, ["drive"])

    server = google.mcp_server(settings)

    assert not server.older and server.services == ("drive",)
    assert server.config["env"]["USER_GOOGLE_EMAIL"] == "ada@example.com"
