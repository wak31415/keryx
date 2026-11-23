"""Keryx's add-an-event plugin, turned on with `keryx plugins install add_calendar_event`.

Its settings are in add_calendar_event.toml beside this file. The code is
`keryx.plugins.calendar`, so an update to Keryx reaches this copy; `keryx plugins remove
add_calendar_event` turns it off.
"""

from pathlib import Path

from keryx.plugins.calendar import add_calendar_event_tool

add_calendar_event = add_calendar_event_tool(Path(__file__).with_suffix(".toml"))
