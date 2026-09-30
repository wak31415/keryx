"""Tests for the read-only cluster stats. No connection is ever opened: the runner is injected.

Two things get most of the attention, because both are the kind of bug that gets read out
loud as fact. The **arithmetic**: a `planned` node is reserved for a queued job rather than
free, a `down` node is not free either, and most pending jobs are blocked on a dependency
rather than competing for GPUs. The fixtures below are synthetic, but they keep every shape
real `sinfo`/`squeue` output takes — both GRES spellings, an `IDX` list with a comma in it,
the trailing `-` that marks a planned node — and the totals asserted on are worked out by
hand from the rows. And the **guard**: an expired login must come back as one spoken status
with no retry, because a retry storm against a dead ControlMaster is how an address gets
banned by a cluster's login nodes.
"""

import subprocess

import pytest

from keryx.integrations.cluster import (
    MARK,
    READ_ONLY,
    ClusterError,
    ClusterSpec,
    ControlMasterSsh,
    GuardedSsh,
    SlurmClusterStats,
    build_script,
    cluster_specs,
    gpu_count,
    parse_duration,
    parse_my_jobs,
    parse_nodes,
    parse_pending,
    split_sections,
    spoken_duration,
    state_tokens,
)

#: The two clusters these tests know. Passed in explicitly: which clusters exist is
#: configuration, never something the module decides.
SPECS = {
    "alpha": ClusterSpec("alpha", "alpha", "shared", "Alpha"),
    "beta": ClusterSpec("beta", "beta", "gpu", "Beta"),
}

# Synthetic, in the shapes real output takes. Alpha spells GRES `gpu:a6000:8`, beta
# `gpu:h100:4(S:0-1)`, and `mixed-` is a node backfill is holding for a queued job.
ALPHA_NODES = """\
alpha-n01              gpu:a6000:8     gpu:a6000:2(IDX:2-3)    mixed
alpha-n02              gpu:a6000:8     gpu:a6000:0(IDX:N/A)    idle
alpha-n03              gpu:a6000:8     gpu:a6000:3(IDX:0-1,5)  mixed
"""

BETA_NODES = """\
beta-g01               gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed
beta-g02               gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed-
beta-g03               gpu:h100:4(S:0-1)   gpu:h100:3(IDX:0,2-3)   mixed-
beta-g04               gpu:h100:4(S:0-1)   gpu:h100:0(IDX:N/A)     down
beta-g05               gpu:h100:4(S:0-1)   gpu:h100:0(IDX:N/A)     planned
beta-g06               gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed
beta-g07               gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed-
beta-g08               gpu:h100:4(S:0-1)   gpu:h100:4(IDX:0-3)     mixed
"""

ALPHA_JOBS = "100042|RUNNING|shared|gres/gpu:2|10:35:03|1|None\n"

BETA_PENDING = """\
200301|Resources
200288_[4-21%4]|JobArrayTaskLimit
200295|Priority
200270|Dependency
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
        ("gpu:a6000:8", 8),  # one spelling of an inventory
        ("gpu:a6000:3(IDX:0-1,5)", 3),  # in use: commas live inside the parens
        ("gpu:h100:4(S:0-1)", 4),  # the other spelling, with a socket suffix
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


def test_a_quiet_partition_adds_up():
    counts, bad = parse_nodes(ALPHA_NODES)

    assert (counts.total, counts.busy, counts.free) == (24, 5, 19)
    assert (counts.reserved, counts.down, counts.nodes) == (0, 0, 3)
    assert bad == []


def test_planned_and_down_gpus_are_never_counted_as_free():
    """23 in use, 0 idle, 5 held (a whole planned node, plus the one spare GPU on a node
    marked `mixed-`), and 4 down."""
    counts, bad = parse_nodes(BETA_NODES)

    assert counts.total == 32
    assert counts.busy == 23
    assert counts.free == 0  # not 5, and certainly not 9
    assert counts.reserved == 5
    assert counts.down == 4
    assert counts.busy + counts.free + counts.reserved + counts.down == counts.total
    assert bad == []


def test_a_node_in_two_partitions_is_counted_once():
    counts, _ = parse_nodes(ALPHA_NODES + "alpha-n01  gpu:a6000:8  gpu:a6000:2(IDX:2-3)  mixed\n")

    assert counts.total == 24


def test_an_unreadable_row_is_reported_rather_than_dropped():
    """A row we cannot read makes the total low; the count of them rides in the payload."""
    counts, bad = parse_nodes(ALPHA_NODES + "alpha-n04 gpu:a6000:8 mixed\n")

    assert counts.total == 24
    assert len(bad) == 1


def test_a_cpu_only_node_is_not_a_node_with_no_gpus():
    counts, bad = parse_nodes(ALPHA_NODES + "alpha-c01  (null)  (null)  idle\n")

    assert counts.nodes == 3
    assert bad == []


def test_my_jobs_split_into_running_and_queued():
    jobs, bad = parse_my_jobs(ALPHA_JOBS)

    assert (jobs.running, jobs.pending, jobs.gpus) == (1, 0, 2)
    assert jobs.ids == [100042]
    assert jobs.soonest_end_s == 38103
    assert bad == []


def test_a_multi_node_job_holds_gpus_per_node():
    """`%b` is TRES *per node*: a two-node job on 4 GPUs each is holding eight."""
    jobs, _ = parse_my_jobs("55|RUNNING|gpu|gres/gpu:4|2:00:00|2|None\n")

    assert jobs.gpus == 8


def test_an_array_job_is_the_number_they_can_repeat_down_the_phone():
    jobs, _ = parse_my_jobs("991_[0-3]|PENDING|shared|gres/gpu:1|1:00:00|1|Priority\n")

    assert jobs.ids == [991]
    assert (jobs.running, jobs.pending) == (0, 1)


def test_the_soonest_finish_is_the_job_that_ends_first():
    jobs, _ = parse_my_jobs(
        "1|RUNNING|gpu|gres/gpu:1|10:00:00|1|None\n2|RUNNING|gpu|gres/gpu:1|0:20:00|1|None\n"
    )

    assert jobs.soonest_end_s == 1200


def test_a_queue_of_dependencies_is_not_a_queue_of_competition():
    """Four pending, of which two are blocked on other jobs rather than on hardware."""
    competing, total = parse_pending(BETA_PENDING)

    assert (competing, total) == (2, 4)


def test_pending_reasons_are_matched_however_slurm_punctuates_them():
    competing, total = parse_pending("1|Dependency\n2|BeginTime\n3|ReqNodeNotAvail, Reserved\n")

    assert (competing, total) == (0, 3)


# --- the script -------------------------------------------------------------


def test_every_command_in_the_script_is_a_slurm_reader():
    """The guard against a future edit that batches a `scancel` in with the reads."""
    script = build_script("shared")

    heads = [part.strip().split()[0] for part in script.split("{ ")[1:]]
    assert heads and set(heads) <= READ_ONLY


def test_the_script_asks_sinfo_per_node_because_the_totals_are_wrong_otherwise():
    assert " -N " in build_script("gpu")


def test_a_partition_that_is_not_a_bare_word_never_reaches_a_shell():
    with pytest.raises(ClusterError) as caught:
        build_script("gpu; rm -rf /")

    assert caught.value.code == "unknown_cluster"


def test_every_known_cluster_builds_a_script():
    for spec in SPECS.values():
        assert spec.partition in build_script(spec.partition)


def test_sections_survive_the_round_trip():
    parsed = split_sections(sections(jobs="a\n", nodes="b\n", pending="c\n"))

    assert parsed == {"jobs": "a", "nodes": "b", "pending": "c"}


# --- the querier ------------------------------------------------------------


async def test_a_whole_report_comes_back_as_a_sentence_worth_saying():
    runner = FakeRunner(sections(jobs=ALPHA_JOBS, nodes=ALPHA_NODES, pending="1|Dependency\n"))

    report = await SlurmClusterStats(runner, SPECS).stats("alpha")

    assert runner.calls[0][0] == "alpha"
    payload = report.as_dict()
    assert payload["gpus_free"] == 19
    assert payload["my_job_ids"] == [100042]
    assert payload["queue_pending"] == 1
    assert payload["queue_waiting_for_hardware"] == 0
    assert payload["spoken"] == (
        "Alpha: 19 of 24 GPUs free; you have 1 job running on 2 GPUs, "
        "the first finishing in about 11 hours."
    )


async def test_a_busy_cluster_says_what_is_held_and_what_is_down():
    runner = FakeRunner(sections(nodes=BETA_NODES, pending=BETA_PENDING))

    report = await SlurmClusterStats(runner, SPECS).stats("beta")

    assert report.spoken() == (
        "Beta: no free GPUs out of 32 (5 held for queued jobs, 4 down); "
        "you have nothing running. 2 other jobs are waiting for hardware."
    )


async def test_a_cluster_it_does_not_know_never_reaches_the_runner():
    """The cluster name is the only thing the model chooses, so it is looked up, not passed."""
    runner = FakeRunner(sections(nodes=ALPHA_NODES))

    with pytest.raises(ClusterError) as caught:
        await SlurmClusterStats(runner, SPECS).stats("gamma")

    assert caught.value.code == "unknown_cluster"
    assert runner.calls == []


async def test_a_name_is_matched_however_they_said_it():
    runner = FakeRunner(sections(nodes=ALPHA_NODES))

    report = await SlurmClusterStats(runner, SPECS).stats("  Alpha ")

    assert report.cluster == "alpha"


async def test_slurms_own_error_is_a_failure_rather_than_an_empty_cluster():
    """`stderr` is folded into the section, so "no nodes" is how a broken command arrives."""
    runner = FakeRunner(sections(nodes="sinfo: error: invalid partition specified: shared\n"))

    with pytest.raises(ClusterError) as caught:
        await SlurmClusterStats(runner, SPECS).stats("alpha")

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

    monkeypatch.setattr("keryx.integrations.cluster.subprocess.run", run)
    return calls


async def test_the_guard_is_called_with_the_host_and_told_not_to_slack(monkeypatch, guard):
    """A login expiry belongs in the sentence they are listening to, not in an unasked-for DM."""
    calls = fake_run(monkeypatch, stdout="out")

    assert await GuardedSsh(guard).run("beta", "sinfo") == "out"

    assert calls[0]["argv"] == [str(guard), "--host", "beta", "sinfo"]
    assert calls[0]["env"]["CLUSTER_SSH_NO_NOTIFY"] == "1"
    assert "shell" not in calls[0]  # the script is one argv element; nothing expands it


async def test_an_expired_login_is_said_once_and_never_retried(monkeypatch, guard):
    """A retry cannot answer a 2FA push, and a storm of them is what a ban counts."""
    calls = fake_run(monkeypatch, returncode=42, stderr="CONTROL_MASTER_EXPIRED host=alpha")

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard).run("alpha", "sinfo")

    assert caught.value.code == "auth_expired"
    assert len(calls) == 1


async def test_an_expiry_is_recognised_from_the_marker_even_on_a_zero_exit(monkeypatch, guard):
    fake_run(monkeypatch, returncode=0, stderr="CONTROL_MASTER_EXPIRED host=alpha")

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard).run("alpha", "sinfo")

    assert caught.value.code == "auth_expired"


async def test_any_other_nonzero_exit_is_unavailable(monkeypatch, guard):
    fake_run(monkeypatch, returncode=1, stderr="boom")

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard).run("alpha", "sinfo")

    assert caught.value.code == "unavailable"


async def test_a_cluster_that_does_not_answer_in_time_is_a_timeout(monkeypatch, guard):
    fake_run(monkeypatch, raises=subprocess.TimeoutExpired("guard", 20))

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard, timeout_s=20).run("alpha", "sinfo")

    assert caught.value.code == "timeout"


async def test_without_the_guard_on_disk_nothing_is_run_at_all(monkeypatch, tmp_path):
    """There is no fallback that dials out by itself — that is the whole point of the guard."""
    calls = fake_run(monkeypatch)

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(tmp_path / "missing.sh").run("alpha", "sinfo")

    assert caught.value.code == "not_configured"
    assert calls == []


async def test_a_host_that_is_not_a_bare_word_is_refused_before_the_guard(monkeypatch, guard):
    calls = fake_run(monkeypatch)

    with pytest.raises(ClusterError) as caught:
        await GuardedSsh(guard).run("alpha; rm -rf /", "sinfo")

    assert caught.value.code == "unknown_cluster"
    assert calls == []


def test_every_failure_has_a_sentence_to_say():
    for code in ("not_configured", "unknown_cluster", "auth_expired", "timeout", "unavailable"):
        assert ClusterError(code).spoken


# --- the built-in guard ------------------------------------------------------
#
# What nobody has to write: `ssh -O check` on the local socket before anything, and a read
# over the live master with nothing that could prompt. Nothing here spawns a real ssh.


def scripted_ssh(monkeypatch, *answers):
    """Stand in for `subprocess.run` with one `(returncode, stdout)` per call, in order."""
    calls: list[list[str]] = []
    queue = list(answers)

    def run(argv, **kwargs):
        calls.append(list(argv))
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        code, out = answer
        return subprocess.CompletedProcess(argv, code, out, "")

    monkeypatch.setattr("keryx.integrations.cluster.subprocess.run", run)
    return calls


async def test_a_dead_master_is_auth_expired_and_no_second_ssh_runs(monkeypatch):
    """The check touches the local socket only; with no master, nothing dials out."""
    calls = scripted_ssh(monkeypatch, (255, ""))

    with pytest.raises(ClusterError) as caught:
        await ControlMasterSsh().run("alpha", "sinfo")

    assert caught.value.code == "auth_expired"
    assert calls == [["ssh", "-O", "check", "alpha"]]


async def test_a_live_master_runs_the_read_in_batch_mode(monkeypatch):
    calls = scripted_ssh(monkeypatch, (0, ""), (0, "out"))

    assert await ControlMasterSsh().run("alpha", "sinfo -N") == "out"

    assert calls[1] == [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "alpha", "sinfo -N"
    ]


async def test_a_master_that_died_during_the_read_is_auth_expired(monkeypatch):
    calls = scripted_ssh(monkeypatch, (0, ""), (255, ""), (255, ""))

    with pytest.raises(ClusterError) as caught:
        await ControlMasterSsh().run("alpha", "sinfo")

    assert caught.value.code == "auth_expired"
    assert len(calls) == 3


async def test_a_failed_read_over_a_live_master_is_unavailable(monkeypatch):
    scripted_ssh(monkeypatch, (0, ""), (1, ""), (0, ""))

    with pytest.raises(ClusterError) as caught:
        await ControlMasterSsh().run("alpha", "sinfo")

    assert caught.value.code == "unavailable"


async def test_a_read_that_hangs_is_a_timeout(monkeypatch):
    scripted_ssh(monkeypatch, (0, ""), subprocess.TimeoutExpired("ssh", 20))

    with pytest.raises(ClusterError) as caught:
        await ControlMasterSsh(timeout_s=20).run("alpha", "sinfo")

    assert caught.value.code == "timeout"


async def test_no_ssh_at_all_is_not_configured(monkeypatch):
    scripted_ssh(monkeypatch, (0, ""), FileNotFoundError("ssh"))

    with pytest.raises(ClusterError) as caught:
        await ControlMasterSsh().run("alpha", "sinfo")

    assert caught.value.code == "not_configured"


async def test_a_check_that_cannot_run_is_a_dead_master(monkeypatch):
    scripted_ssh(monkeypatch, FileNotFoundError("ssh"))

    assert await ControlMasterSsh().master_alive("alpha") is False


async def test_the_built_in_guard_refuses_a_host_that_is_not_a_bare_word(monkeypatch):
    calls = scripted_ssh(monkeypatch)

    with pytest.raises(ClusterError) as caught:
        await ControlMasterSsh().run("alpha; rm -rf /", "sinfo")

    assert caught.value.code == "unknown_cluster"
    assert calls == []


def test_the_configured_names_are_the_whole_of_what_can_be_asked():
    specs = cluster_specs({"alpha": "shared", "beta": "gpu"})

    querier = SlurmClusterStats(ControlMasterSsh(), specs)

    assert querier.known() == ["alpha", "beta"]
    assert querier.resolve("Beta") == ClusterSpec("beta", "beta", "gpu", "Beta")
