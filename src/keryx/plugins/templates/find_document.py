"""Keryx's document answers plugin, turned on with `keryx plugins install find_document`.

Its settings are in find_document.toml beside this file. The code is
`keryx.plugins.documents`, so an update to Keryx reaches this copy; `keryx plugins remove
find_document` turns it off.
"""

from pathlib import Path

from keryx.plugins.documents import find_document_tool

find_document = find_document_tool(Path(__file__).with_suffix(".toml"))
