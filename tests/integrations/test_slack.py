"""Tests for sending Slack messages. No network: the transport is injected."""

import json

from jarvis.integrations.slack import SlackWebApi, slack_credentials

#: A made-up server name: which MCP server carries Slack is the owner's setting, never ours.
SERVER = "team-slack"


def write_config(path, *, token="xoxb-test", channel="D0TEST", server=SERVER):
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    server: {"env": {"SLACK_BOT_TOKEN": token, "SLACK_CHANNEL_ID": channel}}
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_an_explicit_pair_wins(tmp_path):
    config = write_config(tmp_path / "claude.json")

    assert slack_credentials(
        "xoxb-mine", "D0MINE", server=SERVER, config_path=config
    ) == ("xoxb-mine", "D0MINE")


def test_an_explicit_pair_needs_no_server_at_all(tmp_path):
    assert slack_credentials("xoxb-mine", "D0MINE", config_path=tmp_path / "missing.json") == (
        "xoxb-mine",
        "D0MINE",
    )


def test_credentials_fall_back_to_the_named_mcp_server(tmp_path):
    """One Slack app, configured once, wherever it is used from."""
    config = write_config(tmp_path / "claude.json")

    assert slack_credentials(None, None, server=SERVER, config_path=config) == (
        "xoxb-test",
        "D0TEST",
    )


def test_half_a_pair_is_completed_from_the_config(tmp_path):
    config = write_config(tmp_path / "claude.json")

    assert slack_credentials("xoxb-mine", None, server=SERVER, config_path=config) == (
        "xoxb-mine",
        "D0TEST",
    )


def test_without_a_named_server_nothing_is_read_from_the_config(tmp_path):
    """No server name is built in: an unset `SLACK_MCP_SERVER` means no fallback at all."""
    config = write_config(tmp_path / "claude.json")

    assert slack_credentials(None, None, config_path=config) is None
    assert slack_credentials("xoxb-mine", None, server=None, config_path=config) is None


def test_no_slack_anywhere_is_not_an_error(tmp_path):
    missing = tmp_path / "missing.json"
    assert slack_credentials(None, None, server=SERVER, config_path=missing) is None

    unrelated = tmp_path / "other.json"
    write_config(unrelated, server="something-else")
    assert slack_credentials(None, None, server=SERVER, config_path=unrelated) is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert slack_credentials(None, None, server=SERVER, config_path=broken) is None


async def test_send_posts_the_message_to_the_channel():
    posted: list[dict] = []

    def fake_post(url: str, payload: dict, token: str) -> dict:
        posted.append({"url": url, "payload": payload, "token": token})
        return {"ok": True}

    sender = SlackWebApi("xoxb-test", "D0TEST", post=fake_post)

    assert await sender.send("the tests pass") is True
    assert posted[0]["payload"] == {"channel": "D0TEST", "text": "the tests pass"}
    assert posted[0]["token"] == "xoxb-test"


async def test_slack_refusing_the_message_is_a_failure_not_a_crash():
    """Slack reports its own errors in the body, with a 200."""

    def refuse(url: str, payload: dict, token: str) -> dict:
        return {"ok": False, "error": "channel_not_found"}

    assert await SlackWebApi("xoxb-test", "D0TEST", post=refuse).send("hello") is False


async def test_a_transport_failure_is_a_failure_not_a_crash():
    def explode(url: str, payload: dict, token: str) -> dict:
        raise OSError("no route to host")

    assert await SlackWebApi("xoxb-test", "D0TEST", post=explode).send("hello") is False
