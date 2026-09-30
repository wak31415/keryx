"""Jarvis's billing plugin, turned on with `jarvis plugins install check_billing`.

Its settings are in check_billing.toml beside this file. The code is `jarvis.plugins.billing`, so
an update to Jarvis reaches this copy; `jarvis plugins remove check_billing` turns it off.
"""

from pathlib import Path

from jarvis.plugins.billing import check_billing_tool

check_billing = check_billing_tool(Path(__file__).with_suffix(".toml"))
