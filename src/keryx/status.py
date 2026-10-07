"""`keryx status`: one document for the Mac app's menu bar, which asks every 10-30 s.

It is a *report*, never a refusal. A machine that still needs `keryx migrate`, a service
that is not installed, a settings file that will not parse — each is a fact the document
states, in the part it belongs to, and the command still exits 0. So every part is
computed on its own (`_part`): one that raises becomes `{"ok": False, "error": …}` over
that part's defaults (`DEFAULTS`), and the others render as if nothing happened. The
defaults are one table so a failed part can never drift in shape from a good one: the
app's decoder treats every field in it as required.

Cheap on purpose, because of how often it is asked: one SQLite read for every task count,
a bounded loopback GET for the live calls, and service-manager probes that each carry
their own timeout. Everything that leaves the process is a `StatusProbes` field, replaced
wholesale in tests (the `setup.context.Probes` pattern).

No phone number and no secret is in it: the restart record's number stays in the record.
"""

import asyncio
import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from keryx.app import TASK_DB_NAME
from keryx.approvals.audit_log import escalation_enabled
from keryx.config import Settings
from keryx.continuity.memory import memory_path
from keryx.restart import service
from keryx.restart.health import health_probe
from keryx.restart.service import ServiceTarget, resolve_target
from keryx.restart.store import RECORD_NAME, RestartStore
from keryx.restart.version import current_version, running_version
from keryx.tasks.export import told
from keryx.tasks.models import TaskStatus
from keryx.tasks.store import TaskStore

#: The document's version; the Mac app's decoders are written against this one.
SCHEMA = 1
#: The loopback health GET's bound: the app asks every ten seconds, and a Keryx too busy to
#: answer in this long is reported as not answering rather than waited on.
HEALTH_TIMEOUT_S = 1.5
#: How many of the newest tasks the counts are taken over, in one read. Running and queued
#: tasks are always among the newest; a finished one not yet told older than this many
#: others is not counted, which is the price of a read that stays cheap at any size.
TASK_SCAN_LIMIT = 500
#: The `tasks` part's error while the database is still where Jarvis left it.
NEEDS_MIGRATE = "needs keryx migrate"

#: Every part's fields as they read when the part could not be computed. A good part is
#: these, overlaid with what was found; a failed one is exactly these.
DEFAULTS: dict[str, dict[str, Any]] = {
    "storage": {"migration_required": False},
    "service": {"manager": None, "unit": None, "installed": False, "active": False},
    "health": {"reachable": False, "live_sessions": None},
    "version": {"running": None, "checkout": None},
    "restart": {"state": None, "requested_at": None, "failure": None},
    "assistant": {"name": ""},
    "agents": {"default": "", "enabled": []},
    "tasks": {"running": 0, "queued": 0, "unreported": 0},
    "approvals": {"escalation_enabled": True},
    "paths": {
        "config": "", "data": "", "state": "", "cache": "", "logs": "", "memory": "",
    },
}


@dataclass(frozen=True)
class StatusProbes:
    """Everything `build_status` asks outside the settings and the files."""

    target: Callable[[Settings], ServiceTarget | None]
    installed: Callable[[ServiceTarget], bool]
    active: Callable[[ServiceTarget], bool]
    health: Callable[[Settings], int | None]
    running_version: Callable[[Settings], str | None]
    checkout_version: Callable[[Settings], str | None]


def default_probes() -> StatusProbes:
    """The real ones. Asked from outside the unit, as `keryx restart` asks: the app is
    never the service, so an installed unit is the answer."""
    return StatusProbes(
        target=lambda settings: resolve_target(settings, from_outside=True),
        # Late-bound through the module, as `resolve_target` binds them.
        installed=lambda target: service.is_installed(target),
        active=lambda target: service.is_active(target),
        health=lambda settings: health_probe(settings, timeout=HEALTH_TIMEOUT_S),
        running_version=lambda settings: running_version(settings.state_dir),
        checkout_version=lambda settings: current_version(),
    )


def _failed(name: str, error: str) -> dict[str, Any]:
    return {"ok": False, "error": error, **copy.deepcopy(DEFAULTS[name])}


def _part(name: str, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """One part: its defaults overlaid with what `compute` found, or failed with why."""
    try:
        found = compute()
    except Exception as exc:  # noqa: BLE001 - one broken part must not take the document
        return _failed(name, str(exc) or type(exc).__name__)
    return {"ok": True, "error": None, **copy.deepcopy(DEFAULTS[name]), **found}


def _storage(settings: Settings, problems: Mapping[str, str]) -> dict[str, Any]:
    if refusal := settings.storage_refusal():
        return {"ok": False, "error": refusal, "migration_required": True}
    if broken := problems.get("config_file"):
        return {"ok": False, "error": broken}
    return {}


def _service(settings: Settings, probes: StatusProbes) -> dict[str, Any]:
    target = probes.target(settings)
    if target is None:  # nothing supervises it: a supported way to run, not an error
        return {}
    installed = probes.installed(target)
    return {
        "manager": target.manager,
        "unit": target.unit,
        "installed": installed,
        "active": installed and probes.active(target),
    }


def _health(settings: Settings, probes: StatusProbes) -> dict[str, Any]:
    live = probes.health(settings)
    return {"reachable": live is not None, "live_sessions": live}


def _version(settings: Settings, probes: StatusProbes) -> dict[str, Any]:
    return {
        "running": probes.running_version(settings),
        "checkout": probes.checkout_version(settings),
    }


def _restart(settings: Settings) -> dict[str, Any]:
    record = RestartStore(settings.state_dir / RECORD_NAME).load()
    if record is None:
        return {}
    return {
        "state": record.state,
        "requested_at": record.requested_at or None,
        "failure": record.error,
    }


def _assistant(settings: Settings) -> dict[str, Any]:
    return {"name": settings.assistant_name}


def _agents(settings: Settings) -> dict[str, Any]:
    return {"default": settings.agent_backend, "enabled": list(settings.enabled_agents)}


def _tasks(settings: Settings) -> dict[str, Any]:
    """The counts, from one read. Never opens a database `keryx migrate` has yet to move
    (opening one creates it), and never creates one where there is none yet."""
    if settings.storage_refusal():
        return {"ok": False, "error": NEEDS_MIGRATE}
    path = settings.data_dir / TASK_DB_NAME
    if not path.exists():
        return {}

    async def read():
        store = TaskStore(path)
        try:
            return await store.list(limit=TASK_SCAN_LIMIT)
        finally:
            await store.close()

    tasks = asyncio.run(read())
    return {
        "running": sum(task.status == TaskStatus.RUNNING for task in tasks),
        "queued": sum(task.status == TaskStatus.QUEUED for task in tasks),
        "unreported": sum(told(task) is False for task in tasks),
    }


def _approvals(settings: Settings) -> dict[str, Any]:
    return {"escalation_enabled": escalation_enabled(settings.state_dir)}


def _paths(settings: Settings) -> dict[str, Any]:
    return {
        "config": str(settings.config_dir),
        "data": str(settings.data_dir),
        "state": str(settings.state_dir),
        "cache": str(settings.cache_dir),
        "logs": str(settings.state_dir / "logs"),
        "memory": str(memory_path(settings.data_dir)),
    }


def build_status(
    settings: Settings,
    *,
    probes: StatusProbes | None = None,
    config_problems: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The whole document. `config_problems` is what `_load_settings_reporting` gave up on;
    only its `config_file` entry is a storage fact."""
    probes = probes or default_probes()
    problems = config_problems or {}
    # Each part through a lambda, so the module's functions are looked up at call time.
    computed: dict[str, Callable[[], dict[str, Any]]] = {
        "storage": lambda: _storage(settings, problems),
        "service": lambda: _service(settings, probes),
        "health": lambda: _health(settings, probes),
        "version": lambda: _version(settings, probes),
        "restart": lambda: _restart(settings),
        "assistant": lambda: _assistant(settings),
        "agents": lambda: _agents(settings),
        "tasks": lambda: _tasks(settings),
        "approvals": lambda: _approvals(settings),
        "paths": lambda: _paths(settings),
    }
    return {"schema": SCHEMA, **{name: _part(name, computed[name]) for name in DEFAULTS}}


def failed_status(error: str) -> dict[str, Any]:
    """The document when there are no settings to report from at all: every part failed,
    each with the one reason, so the app still has a whole document to decode."""
    return {"schema": SCHEMA, **{name: _failed(name, error) for name in DEFAULTS}}


# --- the text a person reads -----------------------------------------------------------------


LABEL_WIDTH = 15


def _line(label: str, text: str) -> str:
    return f"{label:<{LABEL_WIDTH}}{text}"


def _unknown(part: dict[str, Any]) -> str:
    return f"unknown ({part['error']})"


def _service_text(part: dict[str, Any]) -> str:
    if not part["ok"]:
        return _unknown(part)
    if not part["installed"]:
        return "not installed"
    state = "running" if part["active"] else "stopped"
    return f"{state} ({part['manager']} {part['unit']})"


def _version_text(part: dict[str, Any]) -> str:
    if not part["ok"]:
        return _unknown(part)
    running, checkout = part["running"], part["checkout"]
    text = running or "unknown"
    if checkout and checkout != running:
        text += f" (checkout {checkout})"
    return text


def _calls_text(part: dict[str, Any]) -> str:
    if not part["ok"]:
        return _unknown(part)
    if not part["reachable"]:
        return "not answering"
    return f"{part['live_sessions'] or 'none'} live"


def _tasks_text(part: dict[str, Any]) -> str:
    if not part["ok"]:
        return _unknown(part)
    return (
        f"{part['running']} running, {part['queued']} queued, "
        f"{part['unreported']} not yet told"
    )


def _approvals_text(part: dict[str, Any]) -> str:
    if not part["ok"]:
        return _unknown(part)
    return "on" if part["escalation_enabled"] else "off (kill switch)"


def format_status(report: dict[str, Any]) -> list[str]:
    """`keryx status` without `--json`: five lines, and a sixth when storage needs saying."""
    lines = [
        _line("Keryx", _service_text(report["service"])),
        _line("version", _version_text(report["version"])),
        _line("calls", _calls_text(report["health"])),
        _line("tasks", _tasks_text(report["tasks"])),
        _line("approval calls", _approvals_text(report["approvals"])),
    ]
    if not report["storage"]["ok"]:
        lines.append(_line("storage", report["storage"]["error"]))
    return lines
