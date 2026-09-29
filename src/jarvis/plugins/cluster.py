"""The `cluster_stats` plugin: what the owner's Slurm clusters are doing right now.

Its settings are `cluster_stats.toml`: `[clusters]` (each ssh alias with the partition its
GPUs are in — the alias is also the word they say), an optional `guard` script, and the
wait. Blank `guard` is the built-in one (`integrations.cluster.ControlMasterSsh`), which is
what makes this something anyone with an ssh ControlMaster can turn on: nothing to write.
Whatever the guard, nothing here opens a connection of its own, and a login that has expired
is said, never retried.

Un-PIN-gated, like `check_billing`: three read-only Slurm commands, and a payload of counts
and their own job ids — never a job name, a path or another user. Every cluster is asked at
once, and one being unreachable never costs the others.
"""

import asyncio
import logging
from pathlib import Path

from jarvis import plugins
from jarvis.integrations.cluster import (
    ClusterError,
    ClusterQuerier,
    ClusterReport,
    ControlMasterSsh,
    GuardedSsh,
    SlurmClusterStats,
    cluster_specs,
)

log = logging.getLogger("jarvis.plugins.cluster")

TOOL = "cluster_stats"

#: What the model may say for "every cluster".
ALL_CLUSTERS = ("both", "all", "everything")


def description(names: list[str]) -> str:
    if len(names) == 1:
        subject = f"the Slurm cluster {names[0]} is"
    else:
        subject = f"the Slurm clusters {', '.join(names[:-1])} and {names[-1]} are"
    return (
        f"What {subject} doing right now: free, busy and down GPUs, how many jobs of "
        "theirs are running or queued, how long the first has left, and how busy the queue "
        'is. No PIN needed: it only reads. Call it for "what\'s free on the cluster", "am I '
        'still running", "how busy is the cluster" rather than dispatching; submitting, '
        "cancelling or debugging a job is dispatch_task instead. Say \"one moment\", then the "
        "numbers roughly, once, saying which cluster each belongs to; the free count already "
        "leaves out GPUs that are down or held for a queued job, so do not add them back. If "
        "a cluster comes back with a status other than ok, say the one sentence it gives you "
        "for that one and still report the others."
    )


def parameters(names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "cluster": {
                "type": "string",
                "enum": [*names, "all"],
                "description": "Which cluster. Leave it out for all of them, which is the "
                'right answer when they just say "the cluster".',
            }
        },
        "required": [],
    }


def build_querier(values: dict) -> SlurmClusterStats:
    """The querier the TOML describes; `ToolUnavailable` when it describes none."""
    from jarvis.tools.custom import ToolUnavailable

    if not values["clusters"]:
        raise ToolUnavailable("[clusters] in cluster_stats.toml names no host")
    timeout = float(values["timeout_s"])
    if values["guard"]:
        guard = Path(values["guard"]).expanduser()
        if not guard.is_file():
            raise ToolUnavailable(f"the guard {guard} is not a file (blank it for the built-in)")
        runner = GuardedSsh(guard, timeout_s=timeout)
    else:
        runner = ControlMasterSsh(timeout_s=timeout)
    return SlurmClusterStats(runner, cluster_specs(values["clusters"]))


def cluster_stats_tool(config_path: Path, *, querier: ClusterQuerier | None = None):
    """The tool `cluster_stats.py` defines, configured by the TOML at `config_path`."""
    from jarvis.tools.custom import CustomTool, ToolUnavailable

    try:
        values = plugins.read_config_file(TOOL, config_path)
    except plugins.PluginConfigError as error:
        raise ToolUnavailable(str(error)) from None
    if querier is None:
        querier = build_querier(values)
    names = querier.known()
    if not names:
        raise ToolUnavailable("[clusters] in cluster_stats.toml names no host")

    async def cluster_stats(ctx, arguments: dict) -> dict:
        wanted = str(arguments.get("cluster") or "").strip().lower()
        asked = querier.known() if wanted in ALL_CLUSTERS or not wanted else [wanted]
        results = await asyncio.gather(
            *(querier.stats(name) for name in asked), return_exceptions=True
        )
        reports: list[ClusterReport] = []
        failures: list[dict] = []
        for name, result in zip(asked, results, strict=True):
            if isinstance(result, ClusterReport):
                reports.append(result)
            elif isinstance(result, ClusterError):
                log.warning("cluster %s unavailable: %s (%s)", name, result.code, result.detail)
                failures.append({"cluster": name, "status": result.code, "message": result.spoken})
            elif isinstance(result, BaseException):
                raise result  # the registry turns anything else into {"error": ...}
        if not reports:
            first = failures[0] if failures else {"status": "unavailable", "message": ""}
            return {**first, "unavailable": failures}
        payload = {
            "status": "ok",
            "clusters": [report.as_dict() for report in reports],
            "spoken": " ".join(report.spoken() for report in reports),
        }
        if failures:
            payload["unavailable"] = failures
        return payload

    return CustomTool(
        name=TOOL,
        description=description(names),
        parameters=parameters(names),
        handler=cluster_stats,
        needs_pin=False,
        # Every cluster is asked at once, each stopping itself at `timeout_s` with a
        # sentence to say; this is only the backstop behind them.
        timeout_s=float(values["timeout_s"]) + 5,
    )
