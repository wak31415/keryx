"""Jarvis's cluster stats plugin, turned on with `jarvis plugins install cluster_stats`.

Its settings are in cluster_stats.toml beside this file. The code is `jarvis.plugins.cluster`, so
an update to Jarvis reaches this copy; `jarvis plugins remove cluster_stats` turns it off.
"""

from pathlib import Path

from jarvis.plugins.cluster import cluster_stats_tool

cluster_stats = cluster_stats_tool(Path(__file__).with_suffix(".toml"))
