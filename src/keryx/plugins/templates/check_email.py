"""Keryx's email answers plugin, turned on with `keryx plugins install check_email`.

Its settings are in check_email.toml beside this file. The code is `keryx.plugins.email`, so
an update to Keryx reaches this copy; `keryx plugins remove check_email` turns it off.
"""

from pathlib import Path

from keryx.plugins.email import check_email_tool

check_email = check_email_tool(Path(__file__).with_suffix(".toml"))
