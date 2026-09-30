"""Jarvis's Slack plugin, turned on with `jarvis plugins install send_to_slack`.

Its settings are in send_to_slack.toml beside this file. The code is `jarvis.plugins.slack`, so
an update to Jarvis reaches this copy; `jarvis plugins remove send_to_slack` turns it off.
"""

from pathlib import Path

from jarvis.plugins.slack import send_to_slack_tool

send_to_slack = send_to_slack_tool(Path(__file__).with_suffix(".toml"))
