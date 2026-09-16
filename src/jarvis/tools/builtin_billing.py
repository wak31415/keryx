"""The two tools that read a number and cannot change anything.

`check_billing` reads the provider's own billing API; `cluster_stats` reads Slurm through
an ssh guard script. Neither is PIN-gated, and that is the point: asking what a number is
should not need a PIN, and neither of these can do anything but ask. The read-only
guarantees themselves live in `jarvis/integrations/billing.py` (`_get` takes no method and
no body) and `jarvis/integrations/cluster.py` (`build_script` refuses any command outside
`READ_ONLY`).
"""

import asyncio

from jarvis.integrations.billing import BillingError
from jarvis.integrations.cluster import ClusterError, ClusterQuerier, ClusterReport
from jarvis.tools.builtin_common import (
    ALL_CLUSTERS,
    BillingFactory,
    _text,
    log,
)
from jarvis.tools.registry import ToolContext, ToolRegistry


def register_billing_tools(
    registry: ToolRegistry,
    *,
    billing: BillingFactory | None = None,
    cluster: ClusterQuerier | None = None,
) -> None:
    """Register `check_billing` and `cluster_stats`, each only when it can be answered."""
    # --- check_billing -----------------------------------------------------

    async def check_billing(ctx: ToolContext, arguments: dict) -> dict:
        """This month's spend, read out of the provider's own billing API.

        Read-only end to end, so it is not PIN-gated: it changes nothing, spends a
        fraction of a cent, and "what am I spending" is exactly the sort of small
        question the voice is supposed to answer itself rather than dispatch. Every
        failure comes back as a `status` with a sentence to say, never as a raised
        exception and never with a credential in it.
        """
        assert billing is not None  # only registered when there is one
        provider = _text(arguments, "provider").lower() or None
        try:
            reader = billing(provider)
            report = await reader.month_to_date()
        except BillingError as exc:
            log.warning("billing lookup failed: %s (%s)", exc.code, exc.detail)
            return {"status": exc.code, "message": exc.spoken}
        log.info(
            "billing: %s %.2f %s month to date", report.provider, report.spend, report.currency
        )
        return {"status": "ok", **report.as_dict()}

    if billing is not None:
        registry.register(
            "check_billing",
            "What the API bill is so far this month, and what it is on track to be. Call "
            "it when they ask what they are spending, what the bill looks like, or how much a "
            "provider has cost. It reads the provider's billing API and changes nothing. "
            "Say the figure to the nearest sensible amount rather than every decimal, and "
            "call the month-end number an estimate, because it is a straight-line "
            "projection from the month so far. Default is OpenAI — the account this call "
            "itself runs on; ask for anthropic when they mean what Claude has cost.",
            {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["openai", "anthropic"],
                        "description": "Whose bill. Leave it out for the configured "
                        "default, which is OpenAI.",
                    }
                },
                "required": [],
            },
            check_billing,
        )

    # --- cluster_stats -----------------------------------------------------

    async def cluster_stats(ctx: ToolContext, arguments: dict) -> dict:
        """What the configured clusters are doing right now, straight off Slurm.

        Un-PIN-gated for the same reason as `check_billing`: it is three read-only Slurm
        commands behind the ssh guard, it cannot start, stop or change anything, and the
        numbers it carries are counts and their own job ids — never a job name or a path.
        Every cluster is asked at once, and one being unreachable never costs the others:
        a failure comes back beside the report that worked, as a `status` with a sentence
        to say. See `jarvis/integrations/cluster.py` for why nothing here ever retries an
        expired login.
        """
        assert cluster is not None  # only registered when there is one
        wanted = _text(arguments, "cluster").lower()
        names = cluster.known() if wanted in ALL_CLUSTERS or not wanted else [wanted]
        results = await asyncio.gather(
            *(cluster.stats(name) for name in names), return_exceptions=True
        )

        reports: list[ClusterReport] = []
        failures: list[dict] = []
        for name, result in zip(names, results, strict=True):
            if isinstance(result, ClusterReport):
                reports.append(result)
            elif isinstance(result, ClusterError):
                log.warning("cluster %s unavailable: %s (%s)", name, result.code, result.detail)
                failures.append(
                    {"cluster": name, "status": result.code, "message": result.spoken}
                )
            elif isinstance(result, BaseException):
                raise result  # the registry turns anything else into {"error": ...}

        if not reports:
            first = failures[0] if failures else {"status": "unavailable", "message": ""}
            return {**first, "unavailable": failures}

        log.info("cluster stats for %s", ", ".join(report.cluster for report in reports))
        payload = {
            "status": "ok",
            "clusters": [report.as_dict() for report in reports],
            "spoken": " ".join(report.spoken() for report in reports),
        }
        if failures:
            payload["unavailable"] = failures
        return payload

    # Only with a cluster to ask about: which ones exist is configuration, never built in.
    names = cluster.known() if cluster is not None else []
    if cluster is not None and names:
        if len(names) == 1:
            subject = f"the Slurm cluster {names[0]} is"
        else:
            subject = f"the Slurm clusters {', '.join(names[:-1])} and {names[-1]} are"
        registry.register(
            "cluster_stats",
            f"What {subject} doing right now: free, busy and down "
            "GPUs, how many jobs of theirs are running or queued, and how busy the queue is. "
            'Call it for "what\'s free on the cluster", "am I still running", "how '
            'busy is the cluster", "how long until my job finishes". It only reads Slurm '
            "and changes nothing — submitting, cancelling or debugging a job is "
            "dispatch_task instead. Say the numbers roughly and say which cluster each "
            "one is; the free count already leaves out GPUs that are down or held for a "
            "queued job, so do not add them back. If a cluster comes back with a status "
            "other than ok, say the one thing it tells you to say for that cluster and "
            "still report the others.",
            {
                "type": "object",
                "properties": {
                    "cluster": {
                        "type": "string",
                        "enum": [*names, "all"],
                        "description": "Which cluster. Leave it out for all of them, which "
                        "is the right answer when they just say \"the cluster\".",
                    }
                },
                "required": [],
            },
            cluster_stats,
        )
