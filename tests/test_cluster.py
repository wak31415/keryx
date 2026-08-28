"""Tests for the read-only cluster stats. No connection is ever opened: the runner is injected.

Two things get most of the attention, because both are the kind of bug that gets read out
loud as fact. The **arithmetic**: a `planned` node is reserved for a queued job rather than
free, a `down` node is not free either, and most pending jobs are blocked on a dependency
rather than competing for GPUs — the fixtures below are real `sinfo`/`squeue` output
captured from ionic and tiger on 2026-08-28, and the totals asserted on are the ones the
cluster-compute skill's own `cluster_avail.py` reported for the same moment. And the
**guard**: an expired Duo session must come back as one spoken status with no retry, because
a retry storm against a dead ControlMaster is what got this machine's IP fail2ban-banned.
"""

import subprocess

import pytest

from jarvis.cluster import (
    CLUSTERS,
    MARK,
    READ_ONLY,
    ClusterError,
    GuardedSsh,
    SlurmClusterStats,
    build_script,
    gpu_count,
    parse_duration,
    parse_my_jobs,
    parse_nodes,
    parse_pending,
    split_sections,
    spoken_duration,
    state_tokens,
)

# Real output, captured 2026-08-28. Ionic spells GRES `gpu:a6000:10`, tiger
# `gpu:h100:4(S:0-1)`, and `mixed-` is a node backfill is holding for a queued job.
IONIC_NODES = """\
node209                gpu:a6000:10    gpu:a6000:2(IDX:2-3)    mixed
node300                gpu:a6000:10    gpu:a6000:0(IDX:N/A)    idle
node301                gpu:a6000:10    gpu:a6000:2(IDX:0-1)    mixed
"""

TIGER_NODES = """\
tiger-g06c1g2          gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed
tiger-g06c1g4          gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed-
tiger-g06c2g2          gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed-
tiger-g06c2g4          gpu:h100:4(S:0-1)   gpu:h100:3(IDX:0,2-3)   mixed-
tiger-g06c3g2          gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed-
tiger-g06c3g4          gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed
tiger-g06c4g2          gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed-
tiger-g06c4g4          gpu:h100:4(S:0-1)   gpu:h100:0(IDX:N/A)     down
tiger-g06c5g2          gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed-
tiger-g06c5g4          gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed
tiger-g06c6g2          gpu:h100:4(S:0-1)   gpu:h100:0(IDX:N/A)     planned
tiger-g06c6g4          gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed-
"""

IONIC_JOBS = "30872197|RUNNING|pci|gres/gpu:2|10:35:03|1|None\n"

TIGER_PENDING = """\
3957183|Resources
3956813_[4-21%4]|JobArrayTaskLimit
3957111|Priority
3949632|Dependency
"""


def sections(*, jobs: str = "", nodes: str = "", pending: str = "") -> str:
    """The three marker-delimited sections, as the remote script writes them."""
    return (
        f"{MARK}jobs\n{jobs}{MARK}nodes\n{nodes}{MARK}pending\n{pending}{MARK}__end__\n"
    )


class FakeRunner:
    """A `RemoteRunner` answering from a script; records every host it was asked about."""

    def __init__(self, output: str | ClusterError = "") -> None:
        self.output = output
        self.calls: list[tuple[str, str]] = []

    async def run(self, host: str, script: str) -> str:
        self.calls.append((host, script))
        if isinstance(self.output, ClusterError):
            raise self.output
        return self.output


# --- GRES, states and durations --------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gpu:a6000:10", 10),  # ionic inventory
        ("gpu:a6000:2(IDX:2-3)", 2),  # ionic in-use: commas live inside the parens
        ("gpu:h100:4(S:0-1)", 4),  # tiger inventory
        ("gpu:h100:0(IDX:N/A)", 0),
        ("gres/gpu:2", 2),  # squeue's `%b`
        ("gpu:10", 10),
        ("gpu:2,gpu:3", 5),
        ("(null)", 0),
        ("N/A", 0),
        ("", 0),
        ("billing=1", 0),
    ],
)
def test_gpu_count_reads_either_clusters_spelling(text, expected):
    """No GPU model is hardcoded: the model name is whatever sits between `gpu` and the count."""
    assert gpu_count(text) == expected


def test_a_gres_entry_with_no_number_raises_rather_than_counting_zero():
    """A silent zero here is read out loud as "nothing running", which is worse than an error."""
    with pytest.raises(ValueError):
        gpu_count("gpu:h100")


def test_state_flag_characters_are_not_part_of_the_state_name():
    """`mixed-` is PLANNED and `idle*` is not responding; miss the flag and both read as free."""
    assert state_tokens("mixed-") == ["planned", "mixed"]
    assert state_tokens("idle*") == ["no_respond", "idle"]
    assert state_tokens("MIXED+PLANNED") == ["mixed", "planned"]
    assert state_tokens("") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10:35:03", 38103),
        ("1-10:35:03", 124503),
        ("45:00", 2700),
        ("30", 30),
        ("UNLIMITED", None),
        ("INVALID", None),
        ("", None),
    ],
)
def test_parse_duration_reads_slurms_clock(text, expected):
    assert parse_duration(text) == expected


def test_a_duration_is_spoken_roughly_or_not_at_all():
    assert spoken_duration(None) is None
    assert spoken_duration(60) == "about 1 minute"
    assert spoken_duration(2700) == "about 45 minutes"
    assert spoken_duration(38103) == "about 11 hours"
    assert spoken_duration(5 * 86400) == "about 5 days"


# --- the arithmetic ---------------------------------------------------------


def test_ionic_nodes_come_out_as_the_cluster_reported_them():
    counts, bad = parse_nodes(IONIC_NODES)

    assert (counts.total, counts.busy, counts.free) == (30, 4, 26)
    assert (counts.reserved, counts.down, counts.nodes) == (0, 0, 3)
    assert bad == []


def test_planned_and_down_gpus_are_never_counted_as_free():
    """The same moment `cluster_avail.py` read as 39 in use, 0 idle, 5 planned, 4 down."""
    counts, bad = parse_nodes(TIGER_NODES)

    assert counts.total == 48
    assert counts.busy == 39
    assert counts.free == 0  # not 5, and certainly not 9
    assert counts.reserved == 5
    assert counts.down == 4
    assert counts.busy + counts.free + counts.reserved + counts.down == counts.total
    assert bad == []


def test_a_node_in_two_partitions_is_counted_once():
    counts, _ = parse_nodes(IONIC_NODES + "node209  gpu:a6000:10  gpu:a6000:2(IDX:2-3)  mixed\n")

    assert counts.total == 30


def test_an_unreadable_row_is_reported_rather_than_dropped():
    """A row we cannot read makes the total low; the count of them rides in the payload."""
    counts, bad = parse_nodes(IONIC_NODES + "node302 gpu:a6000:8 mixed\n")

    assert counts.total == 30
    assert len(bad) == 1


def test_a_cpu_only_node_is_not_a_node_with_no_gpus():
    counts, bad = parse_nodes(IONIC_NODES + "cpu001  (null)  (null)  idle\n")

    assert counts.nodes == 3
    assert bad == []


def test_my_jobs_split_into_running_and_queued():
    jobs, bad = parse_my_jobs(IONIC_JOBS)

    assert (jobs.running, jobs.pending, jobs.gpus) == (1, 0, 2)
    assert jobs.ids == [30872197]
    assert jobs.soonest_end_s == 38103
    assert bad == []


def test_a_multi_node_job_holds_gpus_per_node():
    """`%b` is TRES *per node*: a two-node job on 4 GPUs each is holding eight."""
    jobs, _ = parse_my_jobs("55|RUNNING|gpu|gres/gpu:4|2:00:00|2|None\n")

    assert jobs.gpus == 8


def test_an_array_job_is_the_number_he_can_repeat_down_the_phone():
    jobs, _ = parse_my_jobs("991_[0-3]|PENDING|pci|gres/gpu:1|1:00:00|1|Priority\n")

    assert jobs.ids == [991]
    assert (jobs.running, jobs.pending) == (0, 1)


def test_the_soonest_finish_is_the_job_that_ends_first():
    jobs, _ = parse_my_jobs(
        "1|RUNNING|pci|gres/gpu:1|10:00:00|1|None\n2|RUNNING|pci|gres/gpu:1|0:20:00|1|None\n"
    )

    assert jobs.soonest_end_s == 1200


def test_a_queue_of_dependencies_is_not_a_queue_of_competition():
    """Four pending, of which two are blocked on other jobs rather than on hardware."""
    competing, total = parse_pending(TIGER_PENDING)

    assert (competing, total) == (2, 4)


def test_pending_reasons_are_matched_however_slurm_punctuates_them():
    competing, total = parse_pending("1|Dependency\n2|BeginTime\n3|ReqNodeNotAvail, Reserved\n")

    assert (competing, total) == (0, 3)


# --- the script -------------------------------------------------------------


def test_every_command_in_the_script_is_a_slurm_reader():
    """The guard against a future edit that batches a `scancel` in with the reads."""
    script = build_script("pci")

    heads = [part.strip().split()[0] for part in script.split("{ ")[1:]]
    assert heads and set(heads) <= READ_ONLY


def test_the_script_asks_sinfo_per_node_because_the_totals_are_wrong_otherwise():
    assert " -N " in build_script("gpu")


def test_a_partition_that_is_not_a_bare_word_never_reaches_a_shell():
    with pytest.raises(ClusterError) as caught:
        build_script("gpu; rm -rf /")

    assert caught.value.code == "unknown_cluster"


def test_every_known_cluster_builds_a_script():
    for spec in CLUSTERS.values():
        assert spec.partition in build_script(spec.partition)


def test_sections_survive_the_round_trip():
    parsed = split_sections(sections(jobs="a\n", nodes="b\n", pending="c\n"))

    assert parsed == {"jobs": "a", "nodes": "b", "pending": "c"}


# --- the querier ------------------------------------------------------------


async def test_a_whole_report_comes_back_as_a_sentence_worth_saying():
    runner = FakeRunner(sections(jobs=IONIC_JOBS, nodes=IONIC_NODES, pending="1|Dependency\n"))

    report = await SlurmClusterStats(runner).stats("ionic")

    assert runner.calls[0][0] == "ionic"
    payload = report.as_dict()
    assert payload["gpus_free"] == 26
    assert payload["my_job_ids"] == [30872197]
    assert payload["queue_pending"] == 1
    assert payload["queue_waiting_for_hardware"] == 0
    assert payload["spoken"] == (
        "Ionic: 26 of 30 GPUs free; you have 1 job running on 2 GPUs, "
        "the first finishing in about 11 hours."
    )


async def test_a_busy_cluster_says_what_is_held_and_what_is_down():
    runner = FakeRunner(sections(nodes=TIGER_NODES, pending=TIGER_PENDING))

    report = await SlurmClusterStats(runner).stats("tiger")

    assert report.spoken() == (
        "Tiger: no free GPUs out of 48 (5 held for queued jobs, 4 down); "
        "you have nothing running. 2 other jobs are waiting for hardware."
    )


async def test_a_cluster_it_does_not_know_never_reaches_the_runner():
    """The cluster name is the only thing the model chooses, so it is looked up, not passed."""
    runner = FakeRunner(sections(nodes=IONIC_NODES))

    with pytest.raises(ClusterError) as caught:
        await SlurmClusterStats(runner).stats("della")

    assert caught.value.code == "unknown_cluster"
    assert runner.calls == []


async def test_a_name_is_matched_however_he_said_it():
    runner = FakeRunner(sections(nodes=IONIC_NODES))

    report = await SlurmClusterStats(runner).stats("  Ionic ")

    assert report.cluster == "ionic"


async def test_slurms_own_error_is_a_failure_rather_than_an_empty_cluster():
    """`stderr` is folded into the section, so "no nodes" is how a broken command arrives."""
    runner = FakeRunner(sections(nodes="sinfo: error: invalid partition specified: pci\n"))

    with pytest.raises(ClusterError) as caught:
        await SlurmClusterStats(runner).stats("ionic")

    assert caught.value.code == "unavailable"


# --- the guard --------------------------------------------------------------


@pytest.fixture
def guard(tmp_path):
    """A stand-in for `cluster_ssh.sh` on disk; nothing ever runs it (see `fake_run`)."""
    path = tmp_path / "cluster_ssh.sh"
    path.write_text("#!/bin/sh\nexit 0\n")
    return path


def fake_run(monkeypatch, *, returncode=0, stdout="", stderr="", raises=None):
    """Stand in for `subprocess.run`, recording every call. Nothing is ever spawned."""
    calls: list[dict] = []

    def run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr("jarvis.cluster.subprocess.run", run)
    return calls


async def test_the_guard_is_called_with_the_host_and_told_not_to_slack(monkeypatch, guard):
    """The Duo expiry belongs in the sentence he is listening to, not in an unasked-for DM."""
    calls = fake_run(monkeypatch, stdout="out")

    assert await GuardedSsh(guard).run("tiger", "sinfo") == "out"

    assert calls[0]["argv"] == [str(guard), "--host", "tiger", "sinfo"]
    assert calls[0]["env"]["CLUSTER_SSH_NO_NOTIFY"] == "1"
    assert "shell" not in calls[0]  # the script is one argv element; nothing expands it


async def test_an_expired_duo_session_is_said_once_and_never_retried(monkeypatch, guard):
    """A retry cannot answer a Duo push, and a storm of them is what fail2ban counts."""
    calls = fake_run(monkeypatch, returncode=42, stderr="CONTROL_MASTER_EXPIRED host=ionic")

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard).run("ionic", "sinfo")

    assert caught.value.code == "auth_expired"
    assert len(calls) == 1


async def test_an_expiry_is_recognised_from_the_marker_even_on_a_zero_exit(monkeypatch, guard):
    fake_run(monkeypatch, returncode=0, stderr="CONTROL_MASTER_EXPIRED host=ionic")

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard).run("ionic", "sinfo")

    assert caught.value.code == "auth_expired"


async def test_any_other_nonzero_exit_is_unavailable(monkeypatch, guard):
    fake_run(monkeypatch, returncode=1, stderr="boom")

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard).run("ionic", "sinfo")

    assert caught.value.code == "unavailable"


async def test_a_cluster_that_does_not_answer_in_time_is_a_timeout(monkeypatch, guard):
    fake_run(monkeypatch, raises=subprocess.TimeoutExpired("guard", 20))

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard, timeout_s=20).run("ionic", "sinfo")

    assert caught.value.code == "timeout"


async def test_without_the_guard_on_disk_nothing_is_run_at_all(monkeypatch, tmp_path):
    """There is no fallback that dials out by itself — that is the whole point of the guard."""
    calls = fake_run(monkeypatch)

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(tmp_path / "missing.sh").run("ionic", "sinfo")

    assert caught.value.code == "not_configured"
    assert calls == []


async def test_a_host_that_is_not_a_bare_word_is_refused_before_the_guard(monkeypatch, guard):
    calls = fake_run(monkeypatch)

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard).run("ionic; rm -rf /", "sinfo")

    assert caught.value.code == "unknown_cluster"
    assert calls == []


def test_every_failure_has_a_sentence_to_say():
    for code in ("not_configured", "unknown_cluster", "auth_expired", "timeout", "unavailable"):
        assert ClusterError(code).spoken
