"""Keryx's cluster stats plugin, turned on with `keryx plugins install cluster_stats`.

Its settings are in cluster_stats.toml beside this file. The code is `keryx.plugins.cluster`, so
an update to Keryx reaches this copy; `keryx plugins remove cluster_stats` turns it off.
"""

from pathlib import Path

from keryx.plugins.cluster import cluster_stats_tool

cluster_stats = cluster_stats_tool(Path(__file__).with_suffix(".toml"))
