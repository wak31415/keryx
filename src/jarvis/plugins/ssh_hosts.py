"""The ssh hosts `cluster_stats` could ask, read from `~/.ssh/config` — never a connection.

`discover` reads the concrete `Host` aliases (following `Include`, globs and all, relative
to `~/.ssh`; skipping `*`, `?` and `!` patterns) and asks `ssh -G ALIAS` for each, which
prints the effective configuration across `Match` blocks and wildcards without touching the
network. That says whether the alias has a ControlMaster, and a host without one is never
offered: Jarvis only ever rides a login the owner already has open.

`master_alive` is `ssh -O check`, the local control socket and nothing else; `partitions`
reads the Slurm partitions through the built-in guard, so only over a live master.
"""

import asyncio
import glob
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from jarvis.integrations.cluster import ClusterError, ControlMasterSsh

Run = Callable[..., subprocess.CompletedProcess]

#: What `ssh -G` may take; it reads files and resolves nothing over the network.
CONFIG_TIMEOUT_S = 5.0
#: How deep `Include` may nest before it is taken for a loop.
MAX_DEPTH = 8
#: `ControlMaster` values that open (or offer to open) a master.
MASTER_ON = frozenset({"yes", "auto", "ask", "autoask"})


@dataclass(frozen=True)
class SshHost:
    """One alias, as ssh itself would resolve it."""

    alias: str
    hostname: str
    user: str
    control_master: bool
    control_path: str

    def as_dict(self) -> dict:
        return asdict(self)


def default_config() -> Path:
    return Path.home() / ".ssh" / "config"


def _words(line: str) -> tuple[str, list[str]]:
    """A config line as its keyword (lower-cased) and its arguments; `Key=value` too."""
    line = line.strip()
    if not line or line.startswith("#"):
        return "", []
    keyword, _, rest = line.replace("=", " ", 1).partition(" ")
    try:
        arguments = shlex.split(rest, comments=True)
    except ValueError:
        arguments = rest.split()
    return keyword.lower(), arguments


def aliases(config: Path, *, _depth: int = 0, _seen: set[Path] | None = None) -> list[str]:
    """Every concrete `Host` alias in `config` and what it includes, in order, once each."""
    seen = _seen if _seen is not None else set()
    try:
        resolved = config.resolve()
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if resolved in seen or _depth > MAX_DEPTH:
        return []
    seen.add(resolved)
    found: list[str] = []
    for line in text.splitlines():
        keyword, arguments = _words(line)
        if keyword == "host":
            found += [name for name in arguments if not any(c in name for c in "*?!")]
        elif keyword == "include":
            for pattern in arguments:
                path = Path(pattern).expanduser()
                if not path.is_absolute():
                    path = Path.home() / ".ssh" / path
                for match in sorted(glob.glob(str(path))):
                    found += aliases(Path(match), _depth=_depth + 1, _seen=seen)
    return list(dict.fromkeys(found))


def resolve(alias: str, *, config: Path | None = None, run: Run = subprocess.run) -> SshHost:
    """`alias` as `ssh -G` resolves it; an alias ssh cannot resolve has no master."""
    argv = ["ssh", "-G", alias] if config is None else ["ssh", "-F", str(config), "-G", alias]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=CONFIG_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return SshHost(alias, alias, "", False, "")
    effective: dict[str, str] = {}
    if proc.returncode == 0:
        for line in (proc.stdout or "").splitlines():
            key, _, value = line.strip().partition(" ")
            effective.setdefault(key.lower(), value.strip())
    path = effective.get("controlpath", "")
    master = effective.get("controlmaster", "no").lower() in MASTER_ON and path not in ("", "none")
    return SshHost(
        alias=alias,
        hostname=effective.get("hostname", alias),
        user=effective.get("user", ""),
        control_master=master,
        control_path=path if path != "none" else "",
    )


def discover(config: Path | None = None, *, run: Run = subprocess.run) -> list[SshHost]:
    """Every concrete alias in the ssh config, resolved; nothing reaches the network."""
    path = config or default_config()
    explicit = config if config is not None and config != default_config() else None
    return [resolve(alias, config=explicit, run=run) for alias in aliases(path)]


def master_alive(alias: str, *, run: Run = subprocess.run) -> bool:
    """Whether a ControlMaster for `alias` is up now: `ssh -O check`, the local socket only."""
    try:
        proc = run(
            ["ssh", "-O", "check", alias], capture_output=True, text=True, timeout=CONFIG_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def partitions(alias: str, *, ssh: ControlMasterSsh | None = None) -> list[str]:
    """The Slurm partitions on `alias`, read over its live master; [] when it cannot say."""
    try:
        out = asyncio.run((ssh or ControlMasterSsh()).run(alias, "sinfo -h -o %P"))
    except ClusterError:
        return []
    names = [line.strip().rstrip("*") for line in out.splitlines() if line.strip()]
    return list(dict.fromkeys(name for name in names if name))
