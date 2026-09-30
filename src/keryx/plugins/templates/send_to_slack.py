"""Keryx's Slack plugin, turned on with `keryx plugins install send_to_slack`.

Its settings are in send_to_slack.toml beside this file. The code is `keryx.plugins.slack`, so
an update to Keryx reaches this copy; `keryx plugins remove send_to_slack` turns it off.
"""

from pathlib import Path

from keryx.plugins.slack import send_to_slack_tool

send_to_slack = send_to_slack_tool(Path(__file__).with_suffix(".toml"))
