"""Jarvis's email answers plugin, turned on with `jarvis plugins install check_email`.

Its settings are in check_email.toml beside this file. The code is `jarvis.plugins.email`, so
an update to Jarvis reaches this copy; `jarvis plugins remove check_email` turns it off.
"""

from pathlib import Path

from jarvis.plugins.email import check_email_tool

check_email = check_email_tool(Path(__file__).with_suffix(".toml"))
