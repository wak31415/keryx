"""The `cluster_stats` plugin: what the owner's clusters are doing, read-only, no PIN.

The hosts here are made up: nothing in a test knows any real cluster.
"""

import pytest

from keryx import plugins
from keryx.integrations.cluster import (
    MESSAGES,
    ClusterError,
    ClusterReport,
    ControlMasterSsh,
    GpuCounts,
    GuardedSsh,
    MyJobs,
)
from keryx.plugins.cluster import build_querier, cluster_stats_tool
from keryx.tools.custom import ToolUnavailable
from keryx.trust import TrustLevel
from plugins.helpers import call, loaded, names, offered, refusal, turn_on

NAME = "cluster_stats"


class FakeClusters:
    """A `ClusterQuerier` answering from a script: a report, or a `ClusterError` to raise."""

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def known(self) -> list[str]:
        return list(self.answers)

    async def stats(self, cluster: str):
        self.asked.append(cluster)
        answer = self.answers.get(cluster)
        if answer is None:
            raise ClusterError("unknown_cluster", f"no cluster {cluster!r}")
        if isinstance(answer, ClusterError):
            raise answer
        return answer


def a_cluster_report(name: str = "alpha", **overrides) -> ClusterReport:
    defaults = dict(
        cluster=name,
        spoken_name=name.capitalize(),
        partition="shared",
        gpus=GpuCounts(total=30, busy=4, free=26, nodes=3),
        jobs=MyJobs(running=1, gpus=2, soonest_end_s=3600, ids=[100042]),
        queue_competing=0,
        queue_pending=1,
    )
    return ClusterReport(**{**defaults, **overrides})


def both_clusters(**overrides):
    answers = {"alpha": a_cluster_report("alpha"), "beta": a_cluster_report("beta")}
    answers.update(overrides)
    return FakeClusters(answers)


def offer(settings, querier):
    path = plugins.write_config(settings, NAME, {"clusters": {"alpha": "shared"}})
    return offered(cluster_stats_tool(path, querier=querier), settings)


async def test_it_asks_every_cluster_when_they_name_none(settings):
    clusters = both_clusters()

    result = await call(offer(settings, clusters), NAME)

    assert clusters.asked == ["alpha", "beta"]
    assert [entry["cluster"] for entry in result["clusters"]] == ["alpha", "beta"]
    assert "Alpha" in result["spoken"] and "Beta" in result["spoken"]


async def test_it_answers_for_one_cluster_when_they_name_it(settings):
    clusters = both_clusters()

    result = await call(offer(settings, clusters), NAME, {"cluster": "Beta"})

    assert clusters.asked == ["beta"]
    assert [entry["cluster"] for entry in result["clusters"]] == ["beta"]


async def test_the_payload_carries_counts_and_job_ids_but_never_a_job_name(settings):
    result = await call(offer(settings, both_clusters()), NAME, {"cluster": "alpha"})

    entry = result["clusters"][0]
    assert entry["gpus_free"] == 26 and entry["my_job_ids"] == [100042]
    assert "name" not in entry and "cwd" not in entry


async def test_one_cluster_failing_never_costs_the_other(settings):
    querier = both_clusters(beta=ClusterError("auth_expired", "login expired"))

    result = await call(offer(settings, querier), NAME)

    assert result["status"] == "ok"
    assert [entry["cluster"] for entry in result["clusters"]] == ["alpha"]
    assert result["unavailable"] == [
        {"cluster": "beta", "status": "auth_expired", "message": MESSAGES["auth_expired"]}
    ]


@pytest.mark.parametrize(
    "code", ["not_configured", "unknown_cluster", "auth_expired", "timeout", "unavailable"]
)
async def test_every_failure_is_a_status_with_a_sentence(settings, code):
    """Never a raised exception, and never a path or a host for the model to read out."""
    detail = "no guard at /home/someone/bin/guard.sh"
    querier = FakeClusters({name: ClusterError(code, detail) for name in ("alpha", "beta")})

    result = await call(offer(settings, querier), NAME)

    assert (result["status"], result["message"]) == (code, MESSAGES[code])
    assert "/home" not in str(result)


async def test_anything_else_is_an_error_the_registry_speaks(settings):
    class Broken(FakeClusters):
        async def stats(self, cluster):
            raise RuntimeError("bug")

    result = await call(offer(settings, Broken({"alpha": None})), NAME)

    assert "RuntimeError" in result["error"]


async def test_it_needs_no_pin_because_it_only_reads(settings):
    result = await call(offer(settings, both_clusters()), NAME, trust=TrustLevel.NONE)

    assert result["status"] == "ok"


def test_the_schema_offers_exactly_the_configured_clusters(settings):
    schema = offer(settings, both_clusters()).schemas()[0]

    assert schema["parameters"]["properties"]["cluster"]["enum"] == ["alpha", "beta", "all"]
    assert "alpha and beta" in schema["description"]
    assert "No PIN needed" in schema["description"]


def test_one_cluster_is_described_as_one(settings):
    schema = offer(settings, FakeClusters({"alpha": a_cluster_report()})).schemas()[0]

    assert "the Slurm cluster alpha is doing" in schema["description"]


def test_a_querier_that_knows_no_cluster_is_no_tool(settings):
    with pytest.raises(ToolUnavailable, match="names no host"):
        offer(settings, FakeClusters({}))


# --- the querier its file describes -----------------------------------------------------


def test_a_blank_guard_is_the_built_in_one(settings):
    values = plugins.clean(NAME, {"clusters": {"Alpha": "shared"}, "timeout_s": 12})

    querier = build_querier(values)

    assert isinstance(querier.runner, ControlMasterSsh)
    assert querier.runner.timeout_s == 12
    assert querier.known() == ["alpha"]


def test_a_guard_of_their_own_is_used_when_it_is_there(settings, tmp_path):
    guard = tmp_path / "guard.sh"
    guard.write_text("#!/bin/sh\n")
    values = plugins.clean(NAME, {"clusters": {"alpha": "gpu"}, "guard": str(guard)})

    assert isinstance(build_querier(values).runner, GuardedSsh)


def test_a_guard_that_is_not_there_refuses_rather_than_dial_out(settings, tmp_path):
    values = plugins.clean(NAME, {"clusters": {"alpha": "gpu"}, "guard": str(tmp_path / "x")})

    with pytest.raises(ToolUnavailable, match="is not a file"):
        build_querier(values)


# --- installed and loaded ----------------------------------------------------------------


def test_installed_it_loads_from_its_template_and_contacts_nothing(settings):
    turn_on(settings, NAME, clusters={"alpha": "shared", "beta": "gpu"})

    [(_, tool)] = loaded(settings).tools
    assert tool.parameters["properties"]["cluster"]["enum"] == ["alpha", "beta", "all"]
    assert NAME in names(settings)


def test_an_empty_clusters_table_cannot_be_installed(settings):
    with pytest.raises(plugins.PluginConfigError, match="names no host"):
        turn_on(settings, NAME)


def test_emptied_by_hand_the_file_is_refused_with_the_reason(settings):
    turn_on(settings, NAME, clusters={"alpha": "shared"})
    plugins.config_path(settings, NAME).write_text("[clusters]\n")

    assert "names no host" in refusal(settings, NAME)


@pytest.mark.parametrize(
    "table", [{"alpha; rm -rf /": "gpu"}, {"alpha": "gpu && scancel"}, {"alpha": ""}]
)
def test_a_host_or_partition_that_is_not_a_bare_word_is_refused_on_write(settings, table):
    """Both reach a remote shell, so the file can never hold anything else."""
    with pytest.raises(plugins.PluginConfigError, match="bare word"):
        plugins.write_config(settings, NAME, {"clusters": table})
