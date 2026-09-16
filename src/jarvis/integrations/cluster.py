"""What the Slurm clusters are doing right now, read back to the voice model (spec §3.2).

**A worked example, not a feature.** It is one person's answer to "what's free on the
cluster?" and "am I still running?" — one-sentence questions that were costing a whole
subagent and thirty seconds of silence — kept because it shows how a voice tool reaches
outside the machine safely. Nothing here knows any cluster: `CLUSTERS` names them and
`CLUSTER_SSH_GUARD` names the guard, both are empty out of the box, and until both are set
(and the guard is on disk) `build_cluster_stats` builds nothing and the tool is not offered.

Read-only *by construction*, not by intention. `build_script` assembles the remote command
out of module constants and refuses anything whose first word is not in `READ_ONLY`. The
only thing the model chooses is a cluster *name*, which is looked up in the configured set
and refused when it is not there — no string from the model ever reaches a shell. There is
no code path here that can submit a job, cancel one, or write a file.

Three rulings hold it up, and the first is not a preference:

- **Never our own connection.** Where cluster auth is 2FA (a Duo push, say) behind an ssh
  ControlMaster that lasts some hours, a non-interactive process cannot answer the second
  factor: a plain connection attempt against a dead master *hangs*, and a storm of those
  retries is how an address gets banned by the login nodes. So every command goes through
  a guard script, which probes the *local* control socket first — no network, no auth
  attempt — and exits 42 rather than dialling out. Exit 42 means stop, not try again:
  nothing here retries it, and nothing here opens a connection of its own.
- **It speaks, it does not write.** A guard may have its own way of telling the user the
  login has expired. They are on the phone — that is where the sentence belongs — so
  `CLUSTER_SSH_NO_NOTIFY=1` is set and the expiry comes back as a spoken status instead.
- **Numbers, not work.** A report carries GPU counts, queue counts and their own job ids. It
  never carries a job *name*, a path, or another user's name, so the most anyone who got
  past the caller allowlist learns is how busy a machine is. That is also why the tool is
  not PIN-gated: like `check_billing`, it cannot change anything, and asking what a number
  is should not need a PIN.

The guard's contract, for anyone writing one: it is run as `GUARD --host HOST SCRIPT` with
no shell, where `HOST` is a configured cluster name used as an ssh alias; it runs `SCRIPT`
on that host over the existing ControlMaster and prints its output; and when there is no
live master it exits 42 (or writes `CONTROL_MASTER_EXPIRED` to stderr) *without* trying to
authenticate.

The arithmetic follows rules learned the hard way, because the obvious version of it is
wrong: `sinfo` without `-N` aggregates by state line rather than by node (it has reported
fewer than half of a partition's GPUs that way), a `planned` node is reserved for a queued
job rather than free, and most pending jobs are usually blocked on a dependency rather than
competing for hardware. So idle / reserved / down are counted separately and never
collapsed into one "free" number, and pending is classified by reason.

`RemoteRunner` is the seam the tests use: no test ever opens a connection.
"""

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

log = logging.getLogger("jarvis.cluster")


@dataclass(frozen=True)
class ClusterSpec:
    """One cluster: the ssh alias, the partition its GPUs live in, and how to say it."""

    name: str
    host: str
    partition: str
    spoken_name: str


def cluster_specs(clusters: dict[str, str]) -> dict[str, ClusterSpec]:
    """The configured `{name: partition}` as specs: the name doubles as the ssh alias.

    Closed on purpose: this mapping is the *whole* of the model's influence over what
    runs, and a name that is not in it is refused rather than handed to the guard. List
    only the clusters somebody keeps a login open to — one nobody does answers every
    question with "expired".
    """
    return {
        name: ClusterSpec(name, name, partition, name.capitalize())
        for name, partition in clusters.items()
    }


#: The only remote commands that may appear in a batched script. Both are Slurm *readers*
#: with no write mode; `build_script` enforces the list, so a future edit that slips
#: `scancel` into the batch fails a test rather than shipping.
READ_ONLY = frozenset({"squeue", "sinfo"})

#: Only ever a host or a partition out of the configuration, but validated anyway: these
#: are the two values that get as far as a remote shell, and the check costs nothing.
_SHELL_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")

#: Section marker for the batched script: several commands, one round trip, output split
#: back apart on this line.
MARK = "@@JARVIS-CLUSTER@@"

#: The guard exits with this when the ControlMaster is dead.
EXIT_MASTER_EXPIRED = 42

#: How long one cluster's round trip may take. This runs inside a phone call, and the
#: clusters are queried concurrently, so it is the whole wait rather than a sum.
QUERY_TIMEOUT_S = 20.0

ErrorCode = Literal["not_configured", "unknown_cluster", "auth_expired", "timeout", "unavailable"]

#: What the model is told to say, per failure. Written to be spoken, and never naming a
#: path or a host beyond the cluster they already asked about.
MESSAGES: dict[ErrorCode, str] = {
    "not_configured": (
        "cluster access is not set up on this machine; say so plainly and offer to have "
        "Claude wire it up"
    ),
    "unknown_cluster": (
        "that is not one of the clusters you can check; say which ones you can and ask "
        "which the owner meant"
    ),
    "auth_expired": (
        "the cluster login has timed out — they need to log in to it again at their desk "
        "before you can look; say that and offer to try again once they have"
    ),
    "timeout": "the cluster did not answer in time; offer to try again in a moment",
    "unavailable": (
        "the cluster would not answer; say the numbers are not available right now and "
        "offer to put Claude on it"
    ),
}

#: Node-state substrings that mean the GPUs are not available at all. Slurm spells these
#: several ways (drain/drained/draining), so they are matched as substrings.
DOWN_TOKENS = (
    "down",
    "drain",
    "fail",
    "error",
    "maint",
    "unknown",
    "unk",
    "future",
    "power",
    "reboot",
    "invalid",
    "no_respond",
)
#: …and the ones that mean free-but-spoken-for: backfill is holding the node for a queued
#: job. Counting these as free is the single most misleading thing this report could do.
HELD_TOKENS = ("planned", "plnd", "reserved", "resv")

#: `sinfo` appends one flag *character* to the state name. `-` is PLANNED and `*` is NOT
#: RESPONDING; miss them and a reserved or dead node reads as idle.
STATE_FLAGS = {
    "-": "planned",
    "*": "no_respond",
    "~": "power",
    "#": "power",
    "!": "power",
    "%": "power",
    "$": "maint",
    "@": "reboot",
    "^": "reboot",
}

#: Pending reasons that are *not* competing for GPUs — the job is blocked on something
#: other than free hardware, so counting it as contention overstates the queue several
#: times over (36 pending on one real queue, of which exactly 1 was waiting on Resources).
NON_COMPETING_REASONS = frozenset(
    {
        "dependency",
        "dependencyneversatisfied",
        "jobarraytasklimit",
        "jobheldadmin",
        "jobhelduser",
        "begintime",
        "jobholdmaxrequeue",
        "launchfailedrequeuedheld",
        "partitiondown",
        "partitioninactive",
        "accountnotallowed",
        "qosjoblimit",
        "assocmaxjobslimit",
        "qosmaxjobsperuserlimit",
        "reqnodenotavail",
        "maxjobspenuserlimit",
        "none",
        "",
    }
)

_PARENS = re.compile(r"\([^)]*\)")
#: `%i` is `12345` or `12345_[0-3]`; only the leading digits are kept, so an array job
#: comes back as the one number they can repeat down the phone.
_JOB_ID = re.compile(r"^(\d+)")


class ClusterError(Exception):
    """A cluster lookup that failed in a way worth saying out loud.

    `code` is what went wrong, `detail` is for the log, and `spoken` is the sentence handed
    to the voice model. The detail never carries remote output verbatim: a Slurm error can
    be a page long and this one is going to a text-to-speech engine.
    """

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        self.spoken = MESSAGES[code]
        super().__init__(detail or code)


# --- parsing ---------------------------------------------------------------


def gpu_count(field_text: str) -> int:
    """GPUs in a GRES-ish string, however the cluster spells it.

    Handles `gpu:10` and `gres/gpu:2`, `gpu:h100:4(S:0-1)` and `gpu:a6000:2(IDX:2-3)`,
    `(null)` and `N/A`. No GPU model is hardcoded: the
    `(…)` suffix is stripped *first* — IDX lists contain commas — and then the last
    numeric colon-field wins. An entry that names GPUs but carries no number raises, rather
    than counting zero: a silent zero here reads out loud as "nothing running".
    """
    text = (field_text or "").strip()
    if not text or text.lower() in ("(null)", "n/a", "none", "-"):
        return 0
    total = 0
    for raw in _PARENS.sub("", text).split(","):
        entry = raw.strip()
        for prefix in ("gres/", "gres:"):
            if entry.startswith(prefix):
                entry = entry[len(prefix) :]
        parts = [part.strip() for part in entry.split(":")]
        if not parts or parts[0].lower() != "gpu":
            continue
        counts = [part for part in parts[1:] if part.isdigit()]
        if not counts:
            raise ValueError(f"no GPU count in GRES entry {entry!r}")
        total += int(counts[-1])
    return total


def state_tokens(state: str) -> list[str]:
    """`sinfo`'s StateLong split into comparable tokens, trailing flag characters included."""
    tokens: list[str] = []
    for raw in re.split(r"[+,]", (state or "").strip().lower()):
        token = raw.strip()
        while token and token[-1] in STATE_FLAGS:
            tokens.append(STATE_FLAGS[token[-1]])
            token = token[:-1]
        if token:
            tokens.append(token)
    return tokens


def parse_duration(text: str) -> int | None:
    """Slurm's `D-HH:MM:SS` / `HH:MM:SS` / `MM:SS` as seconds; None for `UNLIMITED` etc."""
    raw = (text or "").strip()
    if not raw or not raw[0].isdigit():
        return None
    days, _, clock = raw.rpartition("-")
    parts = clock.split(":")
    if not all(part.isdigit() for part in parts) or not 1 <= len(parts) <= 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds + (int(days) * 86400 if days.isdigit() else 0)


def spoken_duration(seconds: int | None) -> str | None:
    """Seconds as something a voice can say: "about 3 hours", "about 20 minutes"."""
    if seconds is None:
        return None
    if seconds < 90 * 60:
        minutes = max(round(seconds / 60), 1)
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = seconds / 3600
    if hours < 48:
        rounded = round(hours)
        return f"about {rounded} hour{'s' if rounded != 1 else ''}"
    return f"about {round(hours / 24)} days"


@dataclass
class GpuCounts:
    """A partition's GPUs, split four ways. Never collapse these into one number."""

    total: int = 0
    busy: int = 0
    free: int = 0
    #: Idle, but held by backfill for a queued job — free to look at, not to use.
    reserved: int = 0
    down: int = 0
    nodes: int = 0


def parse_nodes(text: str) -> tuple[GpuCounts, list[str]]:
    """`sinfo -N -h -O NodeList,Gres,GresUsed,StateLong` into counts, plus unparsed rows.

    `-N` is mandatory: without it `sinfo` aggregates rows by state line rather than by
    node and every total derived from it is silently wrong. A node listed twice (it is in
    two partitions) is counted once.
    """
    counts = GpuCounts()
    bad: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            bad.append(line.strip())
            continue
        name, gres, gres_used, state = parts
        if name in seen:
            continue
        try:
            total = gpu_count(gres)
            used = gpu_count(gres_used)
        except ValueError as exc:
            bad.append(f"{line.strip()} [{exc}]")
            continue
        seen.add(name)
        if not total:
            continue  # a CPU-only node in a mixed partition
        tokens = state_tokens(state)
        counts.nodes += 1
        counts.total += total
        if any(down in token for token in tokens for down in DOWN_TOKENS):
            counts.down += total
            continue
        counts.busy += min(used, total)
        idle = max(total - used, 0)
        if any(held in token for token in tokens for held in HELD_TOKENS):
            counts.reserved += idle
        else:
            counts.free += idle
    return counts, bad


@dataclass
class MyJobs:
    """Their own jobs on one cluster. Ids, never names — see the module docstring."""

    running: int = 0
    pending: int = 0
    gpus: int = 0
    #: Time left on the running job that finishes first, in seconds.
    soonest_end_s: int | None = None
    ids: list[int] = field(default_factory=list)


def parse_my_jobs(text: str) -> tuple[MyJobs, list[str]]:
    """`squeue -h -u $USER -o "%i|%T|%P|%b|%L|%D|%r"` into their side of the report."""
    jobs = MyJobs()
    bad: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 6)
        if len(parts) != 7:
            bad.append(line.strip())
            continue
        raw_id, state, _partition, gres, left, nodes, _reason = parts
        match = _JOB_ID.match(raw_id.strip())
        if match:
            jobs.ids.append(int(match.group(1)))
        state = state.strip().upper()
        if state == "RUNNING":
            jobs.running += 1
            try:
                per_node = gpu_count(gres)
            except ValueError as exc:
                bad.append(f"{line.strip()} [{exc}]")
                per_node = 0
            # `%b` is TRES *per node*, so a two-node job holds twice what it says.
            jobs.gpus += per_node * (int(nodes.strip()) if nodes.strip().isdigit() else 1)
            remaining = parse_duration(left)
            if remaining is not None and (
                jobs.soonest_end_s is None or remaining < jobs.soonest_end_s
            ):
                jobs.soonest_end_s = remaining
        elif state == "PENDING":
            jobs.pending += 1
    return jobs, bad


def parse_pending(text: str) -> tuple[int, int]:
    """`squeue -t PD -h -o "%i|%r"` as `(competing, total)` pending jobs.

    Competing means "waiting for hardware". A `Dependency` or `JobArrayTaskLimit` job is
    not in the running for the owner's GPUs, and counting it as if it were turns a quiet queue
    into a crowded one.
    """
    total = competing = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        _, _, reason = line.partition("|")
        total += 1
        # Slurm qualifies some reasons with a list (`ReqNodeNotAvail, UnavailableNodes:…`);
        # the head is the reason, and the tail is which nodes.
        key = re.sub(r"[^a-z]", "", reason.split(",")[0].strip().lower())
        if key not in NON_COMPETING_REASONS:
            competing += 1
    return competing, total


# --- the report ------------------------------------------------------------


@dataclass
class ClusterReport:
    """One cluster's numbers, in the shape the voice tool hands back."""

    cluster: str
    spoken_name: str
    partition: str
    gpus: GpuCounts
    jobs: MyJobs
    queue_competing: int = 0
    queue_pending: int = 0
    #: Rows Slurm returned that did not parse. Carried into the payload so a total that is
    #: quietly low is visible rather than believed.
    unparsed: int = 0

    def _gpu_phrase(self) -> str:
        gpus = self.gpus
        if gpus.total == 0:
            return "no GPU information came back"
        if gpus.free:
            phrase = f"{gpus.free} of {gpus.total} GPUs free"
        else:
            phrase = f"no free GPUs out of {gpus.total}"
        extra = []
        if gpus.reserved:
            extra.append(f"{gpus.reserved} held for queued jobs")
        if gpus.down:
            extra.append(f"{gpus.down} down")
        return phrase + (f" ({', '.join(extra)})" if extra else "")

    def _jobs_phrase(self) -> str:
        jobs = self.jobs
        if not jobs.running and not jobs.pending:
            return "you have nothing running"
        if not jobs.running:
            plural = "s" if jobs.pending != 1 else ""
            return f"you have {jobs.pending} job{plural} queued and nothing running"
        phrase = f"you have {jobs.running} job{'s' if jobs.running != 1 else ''} running"
        if jobs.gpus:
            phrase += f" on {jobs.gpus} GPU{'s' if jobs.gpus != 1 else ''}"
        soonest = spoken_duration(jobs.soonest_end_s)
        if soonest:
            phrase += f", the first finishing in {soonest}"
        if jobs.pending:
            phrase += f", and {jobs.pending} queued"
        return phrase

    def spoken(self) -> str:
        """One sentence the model can read out without doing arithmetic of its own."""
        line = f"{self.spoken_name}: {self._gpu_phrase()}; {self._jobs_phrase()}"
        if self.queue_competing:
            others = "s are" if self.queue_competing != 1 else " is"
            line += f". {self.queue_competing} other job{others} waiting for hardware"
        return line + "."

    def as_dict(self) -> dict:
        """The tool's payload for one cluster. Ids and counts; no names, no paths."""
        return {
            "cluster": self.cluster,
            "partition": self.partition,
            "gpus_total": self.gpus.total,
            "gpus_free": self.gpus.free,
            "gpus_busy": self.gpus.busy,
            "gpus_reserved": self.gpus.reserved,
            "gpus_down": self.gpus.down,
            "nodes": self.gpus.nodes,
            "my_jobs_running": self.jobs.running,
            "my_jobs_pending": self.jobs.pending,
            "my_gpus": self.jobs.gpus,
            "my_job_ids": self.jobs.ids,
            "my_soonest_finish": spoken_duration(self.jobs.soonest_end_s),
            "queue_pending": self.queue_pending,
            "queue_waiting_for_hardware": self.queue_competing,
            "unparsed_rows": self.unparsed,
            "spoken": self.spoken(),
        }


# --- transport -------------------------------------------------------------


def build_script(partition: str) -> str:
    """The one batched, marker-delimited command for a cluster. Reads only.

    Three Slurm reads in a single round trip, because the guard's rule is one call per
    question and never a poll loop. `stderr` is folded into each section so a command that
    failed is *visible* to the parser rather than an empty section that reads as "nothing
    running". Every command is checked against `READ_ONLY` before it is returned.
    """
    if not _SHELL_SAFE.match(partition):
        raise ClusterError("unknown_cluster", f"unsafe partition {partition!r}")
    commands = [
        # Their jobs across the whole cluster, not just this partition: they ask "am I still
        # running", and a job they put on some other partition is still a job.
        ("jobs", 'squeue -h -u "$USER" -o "%i|%T|%P|%b|%L|%D|%r"'),
        # `-N` is mandatory; see `parse_nodes`.
        ("nodes", f'sinfo -p {partition} -N -h -O "NodeList:40,Gres:64,GresUsed:80,StateLong:24"'),
        ("pending", f'squeue -p {partition} -t PD -h -o "%i|%r"'),
    ]
    parts: list[str] = []
    for name, command in commands:
        head = command.split(maxsplit=1)[0]
        if head not in READ_ONLY:
            raise ClusterError("unavailable", f"refusing non-read-only command {head!r}")
        parts.append(f'printf "%s\\n" {MARK}{name}')
        parts.append(f"{{ {command} ; }} 2>&1")
    parts.append(f'printf "%s\\n" {MARK}__end__')
    return "; ".join(parts)


def split_sections(out: str) -> dict[str, str]:
    """Marker-delimited output back into `{section: text}`."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in out.splitlines():
        if line.startswith(MARK):
            if current is not None:
                sections[current] = "\n".join(buffer).strip("\n")
            current = line[len(MARK) :].strip()
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip("\n")
    sections.pop("__end__", None)
    return sections


class RemoteRunner(Protocol):
    """Anything that can run one read-only script on a cluster and hand back its output."""

    async def run(self, host: str, script: str) -> str:
        """The script's stdout, or raise `ClusterError`."""
        ...


class GuardedSsh:
    """The guard script as a `RemoteRunner`: the only sanctioned way to reach a cluster.

    The guard is not an implementation detail that could be swapped for a direct
    connection. It checks the *local* control socket before it opens anything, which is
    what makes a dead 2FA session a fast `42` instead of a hang — and a retry storm of
    those hangs is what gets an address banned. A guard that has gone missing since
    startup is `not_configured`; there is no fallback that dials out by itself.
    """

    def __init__(self, guard: Path, *, timeout_s: float = QUERY_TIMEOUT_S) -> None:
        self.guard = Path(guard).expanduser()
        self.timeout_s = timeout_s

    async def run(self, host: str, script: str) -> str:
        if not _SHELL_SAFE.match(host):
            raise ClusterError("unknown_cluster", f"unsafe host {host!r}")
        if not self.guard.is_file():
            raise ClusterError("not_configured", f"no guard at {self.guard}")
        # No shell: the script is one argv element, so nothing local expands it.
        argv = [str(self.guard), "--host", host, script]
        # A guard may notify them some other way on expiry; they are on the phone, so we say
        # it instead.
        env = {**os.environ, "CLUSTER_SSH_NO_NOTIFY": "1"}
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClusterError(
                "timeout", f"{host} did not answer in {self.timeout_s:.0f}s"
            ) from exc
        except OSError as exc:
            raise ClusterError(
                "not_configured", f"{type(exc).__name__} running the guard"
            ) from exc
        stderr = proc.stderr or ""
        if proc.returncode == EXIT_MASTER_EXPIRED or "CONTROL_MASTER_EXPIRED" in stderr:
            # Deliberately terminal: a retry cannot answer a second factor, and a storm of
            # them is exactly what gets an address banned.
            raise ClusterError("auth_expired", f"ControlMaster expired on {host}")
        if proc.returncode != 0:
            raise ClusterError("unavailable", f"{host} guard exit {proc.returncode}")
        return proc.stdout or ""


class ClusterQuerier(Protocol):
    """What the voice tool holds: cluster names in, reports (or `ClusterError`) out."""

    def known(self) -> list[str]:
        """Every cluster name this querier will answer for."""
        ...

    async def stats(self, cluster: str) -> ClusterReport:
        """One cluster's numbers, or raise `ClusterError`."""
        ...


class SlurmClusterStats:
    """`ClusterQuerier` over Slurm's read-only commands, one round trip per cluster."""

    def __init__(self, runner: RemoteRunner, clusters: dict[str, ClusterSpec]) -> None:
        self.runner = runner
        self.clusters = clusters

    def known(self) -> list[str]:
        return list(self.clusters)

    def resolve(self, cluster: str) -> ClusterSpec:
        """The spec for a name the model said, or `unknown_cluster`.

        This is the whole of the model's influence over what runs: a name that is not a
        key here never reaches the guard.
        """
        spec = self.clusters.get((cluster or "").strip().lower())
        if spec is None:
            raise ClusterError("unknown_cluster", f"no cluster {cluster!r}")
        return spec

    async def stats(self, cluster: str) -> ClusterReport:
        spec = self.resolve(cluster)
        sections = split_sections(await self.runner.run(spec.host, build_script(spec.partition)))
        gpus, bad_nodes = parse_nodes(sections.get("nodes", ""))
        if gpus.nodes == 0:
            # Slurm's own errors are folded into the section, so an empty partition report
            # is a failure we would otherwise read out as "no GPUs anywhere".
            first = (sections.get("nodes", "").strip().splitlines() or ["no output"])[0]
            raise ClusterError("unavailable", f"{spec.name}: {first[:120]}")
        jobs, bad_jobs = parse_my_jobs(sections.get("jobs", ""))
        competing, pending = parse_pending(sections.get("pending", ""))
        unparsed = bad_nodes + bad_jobs
        if unparsed:
            log.warning(
                "cluster %s: %d unparsed row(s): %s",
                spec.name,
                len(unparsed),
                "; ".join(unparsed[:3]),
            )
        return ClusterReport(
            cluster=spec.name,
            spoken_name=spec.spoken_name,
            partition=spec.partition,
            gpus=gpus,
            jobs=jobs,
            queue_competing=competing,
            queue_pending=pending,
            unparsed=len(unparsed),
        )


def build_cluster_stats(settings) -> SlurmClusterStats | None:
    """The production querier, or None when there is nothing it could answer for.

    None unless clusters are configured *and* the guard named in settings is on disk: a
    tool that could only ever say "not set up" is a tool the model should not be offered.
    """
    guard = settings.cluster_ssh_guard
    if not settings.clusters or guard is None or not Path(guard).expanduser().is_file():
        return None
    return SlurmClusterStats(
        GuardedSsh(guard, timeout_s=settings.cluster_query_timeout_s),
        cluster_specs(settings.clusters),
    )
