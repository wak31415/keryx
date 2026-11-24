"""Keryx's calendar answers plugin, turned on with `keryx plugins install check_calendar`.

Its settings are in check_calendar.toml beside this file. The code is
`keryx.plugins.calendar`, so an update to Keryx reaches this copy; `keryx plugins remove
check_calendar` turns it off.
"""

from pathlib import Path

from keryx.plugins.calendar import check_calendar_tool

check_calendar = check_calendar_tool(Path(__file__).with_suffix(".toml"))
