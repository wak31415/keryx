"""Keryx's Slack messages plugin, turned on with `keryx plugins install slack_message`.

Its settings are in slack_message.toml beside this file. The code is
`keryx.plugins.slack_message`, so an update to Keryx reaches this copy; `keryx plugins
remove slack_message` turns it off.
"""

from pathlib import Path

from keryx.plugins.slack_message import slack_message_tool

slack_message = slack_message_tool(Path(__file__).with_suffix(".toml"))
