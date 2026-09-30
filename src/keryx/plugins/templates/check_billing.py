"""Keryx's billing plugin, turned on with `keryx plugins install check_billing`.

Its settings are in check_billing.toml beside this file. The code is `keryx.plugins.billing`, so
an update to Keryx reaches this copy; `keryx plugins remove check_billing` turns it off.
"""

from pathlib import Path

from keryx.plugins.billing import check_billing_tool

check_billing = check_billing_tool(Path(__file__).with_suffix(".toml"))
