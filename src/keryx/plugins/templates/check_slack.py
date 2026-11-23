"""Keryx's Slack answers plugin, turned on with `keryx plugins install check_slack`.

Its settings are in check_slack.toml beside this file. The code is
`keryx.plugins.slack_check`, so an update to Keryx reaches this copy; `keryx plugins remove
check_slack` turns it off.
"""

from pathlib import Path

from keryx.plugins.slack_check import check_slack_tool

check_slack = check_slack_tool(Path(__file__).with_suffix(".toml"))
