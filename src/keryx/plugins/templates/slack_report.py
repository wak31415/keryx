"""Keryx's Slack plugin, turned on with `keryx plugins install slack_report`.

Its settings are in slack_report.toml beside this file. The code is `keryx.plugins.slack`, so
an update to Keryx reaches this copy; `keryx plugins remove slack_report` turns it off.
"""

from pathlib import Path

from keryx.plugins.slack import slack_report_tool

slack_report = slack_report_tool(Path(__file__).with_suffix(".toml"))
