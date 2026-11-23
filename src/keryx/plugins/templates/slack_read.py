"""Keryx's Slack reading plugin, turned on with `keryx plugins install slack_read`.

Its settings are in slack_read.toml beside this file. The code is
`keryx.plugins.slack_read`, so an update to Keryx reaches this copy; `keryx plugins remove
slack_read` turns it off.
"""

from pathlib import Path

from keryx.plugins.slack_read import slack_read_tool

slack_read = slack_read_tool(Path(__file__).with_suffix(".toml"))
