"""The checks behind `keryx doctor`: is this machine actually able to run Keryx?

`run_doctor_checks` is a pure function over `Settings` (plus the filesystem, `PATH`, the
configuration store and, when one is handed in, a Twilio client) returning a list of
`Check`s, so the CLI is left with printing and an exit code.

Each check belongs to a `section`, named after the `keryx setup` section that would fix
it, so the wizard can read this list to decide what is left to do. And each one that is
not fine is one of two kinds: **missing** (`unset`: nothing is configured yet, which setup
fills in) or **failed** (something is configured and does not work).

Severity: a `hard` failure means Keryx will not work and `doctor` exits non-zero; a
`soft` one is a warning (no mic on this machine, no PIN, no Google) that merely narrows what
Keryx can do. The last few checks are about what Keryx knows and offers rather than
whether it runs — the owner's name, the memory, the projects root, and which plugins are on
and whether each loads — so somebody who did not write it can find out why a tool is missing
without reading the source. The `security` ones look at where
secrets live and who can read them; `keryx doctor --fix` tightens a mode and does nothing
else.
"""

import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from keryx import plugins
from keryx.agents.registry import BACKENDS, auth_status, installed
from keryx.config import (
    DATA_DIR_MODE,
    DATA_FILE_MODE,
    OWNER_FALLBACK,
    PIN_FROM_ENV,
    PIN_FROM_FILE,
    PLACEHOLDER_KEY,
    Settings,
    env_var_name,
    pin_file,
)
from keryx.config.permissions import is_protected
from keryx.config.settings import LEGACY_ENV_FILE
from keryx.config.store import ConfigStore
from keryx.continuity.memory import memory_path, read_memory
from keryx.integrations.gmail import token_path
from keryx.issues import GH_INSTALL_URL, SKILL, gh_status
from keryx.logging_util import mask_number
from keryx.restart.service import INSTALLERS, candidate_target, resolve_target

Severity = Literal["hard", "soft"]

#: The file `doctor` writes and deletes to prove `data_dir` is writable.
WRITE_PROBE_NAME = ".doctor-write-probe"

MARKERS = {"ok": "✅", "hard": "❌", "soft": "⚠️", "optional": "○ "}

#: Tunnels that can put `/twilio/*` in front of Twilio, best first. The deployment uses
#: Cloudflare Tunnel; ngrok still counts, so a dev machine set up before the move passes.
TUNNEL_BINARIES = ("cloudflared", "ngrok")

#: The webhook paths Twilio must be pointed at, under `https://PUBLIC_HOST`.
VOICE_PATH = "/twilio/voice"
STATUS_PATH = "/twilio/status"


@dataclass(frozen=True)
class Check:
    """One diagnostic: what was looked at, whether it is fine, and what was found."""

    name: str
    ok: bool
    detail: str = ""
    severity: Severity = "hard"
    #: The `keryx setup` section that would fix it (`wizard.SECTIONS`), or a label of its own.
    section: str = "service"
    #: Not fine because nothing is configured yet, rather than because something configured
    #: does not work.
    unset: bool = False

    @property
    def state(self) -> str:
        """`ok`, `missing` or `failed` — what `--json` and the wizard read."""
        return "ok" if self.ok else ("missing" if self.unset else "failed")

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "state": self.state}


def format_check(check: Check) -> str:
    """One printable line: a marker, the check's name, and the detail behind it."""
    if check.ok:
        marker = MARKERS["ok"]
    elif check.unset and check.severity == "soft":
        marker = MARKERS["optional"]
    else:
        marker = MARKERS[check.severity]
    return f"{marker}  {check.name}: {check.detail}" if check.detail else f"{marker}  {check.name}"


def has_hard_failure(checks: list[Check]) -> bool:
    """True when something failed that stops Keryx from working at all."""
    return any(not check.ok and check.severity == "hard" for check in checks)


def run_doctor_checks(
    settings: Settings,
    *,
    config_problems: Mapping[str, str] | None = None,
    store: ConfigStore | None = None,
    twilio: Any | None = None,
) -> list[Check]:
    """Every check, in the order they are printed. Never raises: problems come back as checks.

    `config_problems` maps a `Settings` field name to why its configured value was refused,
    for the fields the CLI had to replace to load at all (see `cli.DOCTOR_FALLBACKS`).
    Without it a rejected value is indistinguishable from an unset one. `twilio` is a
    `notify.twilio_out.TwilioAdmin`; without one the webhook is not looked at.
    """
    problems = config_problems or {}
    store = store or ConfigStore()
    checks = [
        _storage_check(settings),
        _config_check(store),
        _openai_key_check(settings),
        _agent_config_check(settings),
        *_agent_checks(settings),
        _twilio_check(settings),
        _signature_check(settings),
        _public_host_check(settings),
        _tunnel_check(),
    ]
    if twilio is not None:
        checks.append(_webhook_check(settings, twilio))
    checks += [
        _allowed_callers_check(settings),
        _pin_check(settings, problems.get("pin")),
        _owner_name_check(settings),
        service_manager_check(settings),
        _data_dir_check(settings),
        _data_dir_privacy_check(settings),
        _file_modes_check(settings, store),
        _git_check(settings, store),
        _secrets_in_config_check(store),
        _unlocked_check(store),
        _imported_env_check(),
        _google_check(settings),
        _memory_check(settings),
        _projects_root_check(settings),
        *plugin_checks(settings),
        _retired_check(store),
        _issues_check(settings),
    ]
    return checks


# --- configuration ---------------------------------------------------------


def _storage_check(settings: Settings) -> Check:
    """Whether an install from before the XDG layout is still waiting for `keryx migrate`.

    Hard, because `keryx serve` refuses to start in that state (`storage_refusal`), and
    missing rather than failed: nothing is broken, a step has not been taken yet.
    """
    refusal = settings.storage_refusal()
    if refusal is not None:
        return Check("storage", False, refusal, section="import", unset=True)
    return Check("storage", True, "the XDG directories", section="import")


def _config_check(store: ConfigStore) -> Check:
    """Where the configuration lives."""
    where = store.config_path if store.config_path.is_file() else store.home
    return Check("configuration", True, str(where), severity="soft", section="import")


def _openai_key_check(settings: Settings) -> Check:
    """The one required key; `PLACEHOLDER_KEY` means the read-only loader filled it in."""
    key = settings.openai_api_key
    if not key or key == PLACEHOLDER_KEY:
        return Check(
            "OPENAI_API_KEY",
            False,
            "not set — the voice session cannot start",
            section="voice",
            unset=True,
        )
    return Check("OPENAI_API_KEY", True, "set", section="voice")


def _agent_config_check(settings: Settings) -> Check:
    """`AGENT_BACKEND` among `AGENTS_ENABLED`, which `keryx serve` refuses to start without."""
    refusal = settings.agent_refusal()
    if refusal is not None:
        return Check("coding agents", False, refusal, section="agents")
    enabled = ", ".join(settings.enabled_agents)
    return Check(
        "coding agents",
        True,
        f"{settings.agent_backend} by default; enabled: {enabled}",
        section="agents",
    )


def _agent_checks(settings: Settings) -> list[Check]:
    """Per enabled agent: is its CLI installed, and which credential will it run on?

    Hard for the default agent — every task nobody named an agent for goes to it — and
    soft for the rest, which only narrow what can be asked for by name. The stored-login
    probe is a heuristic (an odd Keychain setup could hide a working Claude login), and
    `keryx auth status --smoke` is the test that actually runs one.
    """
    checks = []
    for name in settings.enabled_agents:
        spec = BACKENDS[name]
        severity: Severity = "hard" if name == settings.agent_backend else "soft"
        label = f"{spec.label} agent" + (" (default)" if name == settings.agent_backend else "")
        cli = spec.find_cli()
        if not installed(name):
            checks.append(
                Check(
                    label,
                    False,
                    f"not installed — {spec.install_hint}",
                    severity=severity,
                    section="agents",
                    unset=True,
                )
            )
            continue
        if cli is None:
            detail = f"{name} CLI not found — {spec.install_hint}"
            checks.append(Check(label, False, detail, severity=severity, section="agents"))
            continue
        status = auth_status(name, settings)
        version = spec.cli_version()
        where = f"{cli} ({version})" if version else cli
        checks.append(
            Check(
                label,
                status.ready,
                f"{where}; {status.detail}",
                severity=severity,
                section="agents",
                unset=not status.ready,
            )
        )
    return checks


def _twilio_check(settings: Settings) -> Check:
    """All three Twilio settings, naming the ones that are missing."""
    missing = [
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", settings.twilio_account_sid),
            ("TWILIO_AUTH_TOKEN", settings.twilio_auth_token),
            ("TWILIO_NUMBER", settings.twilio_number),
        )
        if not value
    ]
    if missing:
        return Check(
            "Twilio credentials",
            False,
            f"missing {', '.join(missing)} — no phone channel",
            section="phone",
            unset=True,
        )
    return Check("Twilio credentials", True, mask_number(settings.twilio_number), section="phone")


def _signature_check(settings: Settings) -> Check:
    """Is every Twilio webhook signature-checked? Off behind a tunnel, `serve` refuses.

    Hard when `PUBLIC_HOST` is set, because that is exactly the configuration `keryx serve`
    will not start in; a warning without one, where it is the local-development switch it
    was meant to be.
    """
    if not settings.debug_skip_twilio_validation:
        return Check("Twilio signatures", True, "validated", section="phone")
    refusal = settings.phone_refusal()
    if refusal is not None:
        return Check("Twilio signatures", False, refusal, section="phone")
    return Check(
        "Twilio signatures",
        False,
        "DEBUG_SKIP_TWILIO_VALIDATION is on — for a machine nothing outside can reach",
        severity="soft",
        section="phone",
    )


def _public_host_check(settings: Settings) -> Check:
    if not settings.public_host:
        return Check(
            "PUBLIC_HOST",
            False,
            "not set — Twilio cannot reach this machine",
            section="phone",
            unset=True,
        )
    return Check("PUBLIC_HOST", True, settings.public_host, section="phone")


def _webhook_check(settings: Settings, twilio: Any) -> Check:
    """Is the number pointed at this machine? Read-only, and soft: Twilio may be down.

    Only asked when there is a number and a host to compare; `keryx setup` is what sets
    it, after asking.
    """
    if not (settings.twilio_number and settings.public_host):
        return Check("Twilio webhook", True, "not checked — no number or PUBLIC_HOST yet",
                     severity="soft", section="phone")
    want = f"https://{settings.public_host}{VOICE_PATH}"
    try:
        numbers = {number.phone_number: number for number in twilio.numbers()}
    except Exception as exc:  # a network failure is a warning, never a crash
        return Check("Twilio webhook", False, f"could not ask Twilio: {exc}",
                     severity="soft", section="phone")
    number = numbers.get(settings.twilio_number)
    if number is None:
        return Check(
            "Twilio webhook",
            False,
            f"{mask_number(settings.twilio_number)} is not a number on this Twilio account",
            section="phone",
        )
    if (number.voice_url or "").rstrip("/") != want:
        current = number.voice_url or "nothing"
        return Check(
            "Twilio webhook",
            False,
            f"calls go to {current}, not {want} — `keryx setup` can point it here",
            severity="soft",
            section="phone",
        )
    return Check("Twilio webhook", True, want, severity="soft", section="phone")


def _allowed_callers_check(settings: Settings) -> Check:
    """Without an allowlist every inbound call is refused."""
    if not settings.allowed_callers:
        return Check(
            "allowed callers",
            False,
            "ALLOWED_CALLERS is empty — every call is refused",
            section="owner",
            unset=True,
        )
    return Check(
        "allowed callers",
        True,
        ", ".join(map(mask_number, settings.allowed_callers)),
        section="owner",
    )


def _pin_check(settings: Settings, problem: str | None = None) -> Check:
    """Where the PIN came from, or what to do about there not being one. Never the digits.

    Four states, and they are four different sentences. A malformed `KERYX_PIN` is hard:
    `keryx serve` raises on load, and `doctor` is the command whose whole job is to be
    runnable when nothing else is. No PIN at all is a warning and not a dead end — `keryx
    setup` or the first call can set one, and until then nothing of the owner's is read
    out on the phone. A PIN from the environment is simply set, and so is one in
    `KERYX_HOME/pin`, the PIN's own store. An unusable file is the one dead end left, and
    only the owner can clear it.
    """
    if problem is not None:
        return Check(
            "PIN", False, f"{env_var_name('pin')} is set but unusable — {problem}", section="owner"
        )
    if settings.pin_source == PIN_FROM_ENV:
        return Check(
            "PIN", True, "set from the environment, which wins over KERYX_HOME/pin",
            severity="soft", section="owner",
        )
    path = pin_file(settings.config_dir)
    if settings.pin_source == PIN_FROM_FILE:
        return Check(
            "PIN",
            True,
            f"set on {_enrolled_on(path)}, kept in {path}",
            severity="soft",
            section="owner",
        )
    if settings.pin_enrolment_open:
        return Check(
            "PIN",
            False,
            "no PIN yet — `keryx setup` or the first call can set one, and nothing of "
            "yours is read out until then",
            severity="soft",
            section="owner",
            unset=True,
        )
    return Check(
        "PIN",
        False,
        f"{path} is not 6-8 digits, so there is no PIN and no call can set one — "
        "`keryx setup` replaces it",
        severity="soft",
        section="owner",
    )


def _enrolled_on(path: Path) -> str:
    """The day a PIN was set, from the file's own timestamp. Never its contents."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    except OSError:  # pragma: no cover - it was read moments ago
        return "an unknown date"


def _owner_name_check(settings: Settings) -> Check:
    """Warn-only: without a name the prompts say `OWNER_FALLBACK`, which still works."""
    if not settings.owner_name:
        return Check(
            "owner name",
            False,
            f'{env_var_name("owner_name")} is not set — the prompts call you "{OWNER_FALLBACK}"',
            severity="soft",
            section="owner",
            unset=True,
        )
    return Check("owner name", True, settings.owner_label, severity="soft", section="owner")


# --- the machine -----------------------------------------------------------


def _tunnel_check() -> Check:
    """Is a tunnel binary on `PATH`? Without one Twilio cannot reach this machine."""
    for name in TUNNEL_BINARIES:
        found = shutil.which(name)
        if found:
            return Check("tunnel", True, found, section="phone")
    return Check(
        "tunnel",
        False,
        f"none of {', '.join(TUNNEL_BINARIES)} on PATH — no tunnel for the phone channel",
        section="phone",
        unset=True,
    )


def service_manager_check(settings: Settings) -> Check:
    """Whether a service is installed to supervise Keryx, and what is lost when none is.

    Warn-only: running without one — `SERVICE_MANAGER=none`, no `systemctl`/`launchctl`,
    or simply no unit installed — is a supported way to run Keryx, just a narrower one.
    It is worth saying out loud because the consequence is silent: `keryx restart` and the
    voice model's `restart_service` both refuse, so a subagent that changes Keryx's own
    code has no way to make the change take effect.

    Asked from outside, like `keryx restart`: `doctor` runs in a terminal, never inside
    the unit, so the question is whether the unit is installed — not merely whether its
    manager's command is on PATH, which on Linux it nearly always is.
    """
    candidate = candidate_target(settings)
    target = resolve_target(settings, from_outside=True) if candidate is not None else None
    if target is None:
        if settings.service_manager == "none":
            why = "SERVICE_MANAGER=none"
        elif candidate is None:
            why = "no service manager on this machine"
        else:
            why = f"{candidate.unit} is not installed ({INSTALLERS[candidate.manager]})"
        return Check(
            "service manager",
            False,
            f"{why} — `keryx restart` and the voice's restart_service refuse, and nothing "
            "restarts Keryx if it dies",
            severity="soft",
            section="service",
            unset=True,
        )
    detail = target.describe()
    if shutil.which("git") is None:
        # `current_version` is decoration and degrades quietly; say so once, here, rather
        # than leave someone wondering why every restart reports an unknown version.
        detail += " — but no git on PATH, so versions will report as unknown"
    return Check("service manager", True, detail, severity="soft", section="service")


def _data_dir_check(settings: Settings) -> Check:
    """Can we actually write tasks, transcripts and logs where they are meant to go?"""
    path = settings.data_dir
    probe = path / WRITE_PROBE_NAME
    try:
        # `mode=` rather than `secure_dir`: `doctor` must not leave a world-readable
        # data directory behind on a machine that did not have one, and it must not quietly
        # tighten one that does — the privacy check's job is to report what is there.
        path.mkdir(mode=DATA_DIR_MODE, parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("data dir writable", False, f"{path}: {exc}", section="security")
    return Check("data dir writable", True, str(path), section="security")


def _data_dir_privacy_check(settings: Settings) -> Check:
    """Who besides the owner can read the call transcripts.

    Warn-only: on a single-user machine a loose mode costs nothing, and refusing to run
    over it would be out of proportion. On a shared host it is the whole story —
    `calls/*.log` is every word of every call. `ensure_dirs` tightens the directory on
    every start, so a warning here means something else loosened it afterwards.
    """
    path = settings.data_dir
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return Check("data dir private", False, f"{path}: {exc}", severity="soft",
                     section="security")

    exposed = mode & (stat.S_IRWXG | stat.S_IRWXO)
    if exposed:
        return Check(
            "data dir private",
            False,
            f"{path} is {mode:04o} — transcripts and reports are readable by others; "
            "`keryx doctor --fix` makes it owner-only",
            severity="soft",
            section="security",
        )
    return Check("data dir private", True, f"{mode:04o} (owner only)", severity="soft",
                 section="security")


# --- where the secrets are ---------------------------------------------------


def private_paths(settings: Settings, store: ConfigStore) -> list[tuple[Path, int]]:
    """Every path that holds a secret, with the mode it should have. Only those that exist.

    All four of Keryx's directories are here: the logs quote tool calls and the socket
    beside them is what a keypad approval arrives through, so the state directory is no
    less private than the data one.
    """
    candidates: list[tuple[Path, int]] = [
        (store.home, DATA_DIR_MODE),
        (store.config_path, DATA_FILE_MODE),
        (store.secrets_path, DATA_FILE_MODE),
        (settings.data_dir, DATA_DIR_MODE),
        (settings.state_dir, DATA_DIR_MODE),
        (settings.state_dir / "logs", DATA_DIR_MODE),
        (settings.cache_dir, DATA_DIR_MODE),
        (pin_file(settings.config_dir), DATA_FILE_MODE),
        (token_path(settings), DATA_FILE_MODE),
        (settings.data_dir / "report_secret", DATA_FILE_MODE),
    ]
    if (client := settings.google_client_file()) is not None:
        candidates.append((client, DATA_FILE_MODE))
    google = settings.data_dir / "google"
    if google.is_dir():
        candidates.append((google, DATA_DIR_MODE))
        candidates += [(path, DATA_FILE_MODE) for path in sorted(google.iterdir())
                       if path.is_file()]
    seen: set[Path] = set()
    found = []
    for path, mode in candidates:
        if path.exists() and path not in seen:
            seen.add(path)
            found.append((path, mode))
    return found


def loose_paths(settings: Settings, store: ConfigStore) -> list[tuple[Path, int, int]]:
    """`(path, mode it has, mode it should have)` for each secret others can reach."""
    loose = []
    for path, wanted in private_paths(settings, store):
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:  # pragma: no cover - it existed a moment ago
            continue
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            loose.append((path, mode, wanted))
    return loose


def fix_permissions(settings: Settings, store: ConfigStore) -> list[str]:
    """`keryx doctor --fix`: tighten every loose secret to owner-only. Nothing else.

    Returns a line per path changed, or per path that could not be.
    """
    done = []
    for path, mode, wanted in loose_paths(settings, store):
        try:
            os.chmod(path, wanted)
            done.append(f"{path}: {mode:04o} → {wanted:04o}")
        except OSError as exc:
            done.append(f"{path}: could not change {mode:04o} ({exc})")
    return done


def _file_modes_check(settings: Settings, store: ConfigStore) -> Check:
    loose = loose_paths(settings, store)
    if loose:
        names = ", ".join(f"{path} ({mode:04o})" for path, mode, _ in loose)
        return Check(
            "secret files private",
            False,
            f"readable by others: {names} — `keryx doctor --fix`",
            severity="soft",
            section="security",
        )
    return Check("secret files private", True, "owner only", severity="soft", section="security")


def _inside_git(path: Path) -> Path | None:
    """The work tree `path` is inside, found by looking for `.git` upwards. No subprocess.

    Resolved first, existing or not: `fresh/../jh` has `fresh` among its lexical parents
    and is not inside it.
    """
    path = path.resolve()
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _git_check(settings: Settings, store: ConfigStore) -> Check:
    """A secret under version control is one `git add .` from being pushed somewhere."""
    for path in (store.home, settings.data_dir):
        tree = _inside_git(path)
        if tree is not None:
            return Check(
                "outside git",
                False,
                f"{path} is inside the git work tree at {tree} — move it (KERYX_HOME, "
                "DATA_DIR) somewhere a commit cannot reach",
                severity="soft",
                section="security",
            )
    return Check("outside git", True, "not in a git work tree", severity="soft",
                 section="security")


def _secrets_in_config_check(store: ConfigStore) -> Check:
    try:
        found = store.secrets_in_config()
    except Exception as exc:  # a config.toml that does not parse: `serve` will not start
        return Check("config.toml", False, f"does not parse — {exc}; fix it by hand",
                     section="security")
    if found:
        return Check(
            "config.toml",
            False,
            f"holds {', '.join(found)}, which belong in secrets.toml — "
            f"`keryx config set {found[0]} --stdin` moves one",
            severity="soft",
            section="security",
        )
    return Check("config.toml", True, "no secrets in it", severity="soft", section="security")


def _unlocked_check(store: ConfigStore) -> Check:
    try:
        unlocked = sorted(
            key for key, writable in store.overrides().items() if writable and is_protected(key)
        )
    except Exception:  # reported by `_secrets_in_config_check` already
        unlocked = []
    if unlocked:
        return Check(
            "protected settings",
            False,
            f"config.toml unlocks {', '.join(unlocked)}, which nothing may unlock — ignored, "
            "but somebody tried; remove it from [service_writable]",
            severity="soft",
            section="security",
        )
    return Check("protected settings", True, "none unlocked", severity="soft",
                 section="security")


def _imported_env_check() -> Check:
    leftovers = sorted(Path.cwd().glob(f"{LEGACY_ENV_FILE.name}.imported-*"))
    if leftovers:
        return Check(
            "old .env",
            False,
            f"{', '.join(str(path) for path in leftovers)} still hold the secrets that were "
            "imported — delete them once Keryx runs from the store",
            severity="soft",
            section="security",
        )
    return Check("old .env", True, "none left behind", severity="soft", section="security")


# --- google, and what Keryx knows -----------------------------------------------


def _google_check(settings: Settings) -> Check:
    """Optional, and only about `workspace-mcp` — agents sending mail and using the calendar.

    With it off, Gmail and Calendar reach Claude's subagents through the Claude CLI's own
    claude.ai connectors, which need nothing from us. Codex has no connectors, so with Codex
    enabled, off means its tasks have no mailbox and no calendar at all.
    """
    if not settings.google_workspace_mcp:
        if "codex" in settings.enabled_agents:
            return Check(
                "Google for agents",
                False,
                "not set up (optional) — Codex tasks have no Gmail or Calendar; `keryx "
                "setup` connects Google",
                severity="soft",
                section="google",
                unset=True,
            )
        return Check(
            "Google for agents",
            True,
            "Claude's own connectors carry Gmail and Calendar",
            severity="soft",
            section="google",
        )
    if not settings.google_oauth_client():
        return Check(
            "Google for agents",
            False,
            "GOOGLE_WORKSPACE_MCP is on but there is no OAuth client — `keryx setup`",
            severity="soft",
            section="google",
        )
    credentials_dir = settings.data_dir / "google"
    stored = sorted(credentials_dir.glob("*")) if credentials_dir.is_dir() else []
    if not stored:
        return Check(
            "Google for agents",
            False,
            f"nothing in {credentials_dir} — `keryx auth login google-workspace`",
            severity="soft",
            section="google",
            unset=True,
        )
    return Check("Google for agents", True, str(credentials_dir), severity="soft",
                 section="google")


def _memory_check(settings: Settings) -> Check:
    """Warn-only: a Keryx with no memory opens every call knowing nothing about its owner.

    Read, never created: `doctor` reports what is there.
    """
    memory = read_memory(settings.data_dir)
    if not memory:
        return Check(
            "memory",
            False,
            "nothing remembered yet — `keryx setup` writes a first memory, and every "
            "authorized call adds to it",
            severity="soft",
            section="profile",
            unset=True,
        )
    path = memory_path(settings.data_dir)
    return Check(
        "memory",
        True,
        f"{len(memory)} characters in {path}, sent on every call",
        severity="soft",
        section="profile",
    )


def _projects_root_check(settings: Settings) -> Check:
    """Warn-only, and never created: a missing root sends unscoped tasks to the workspace."""
    root = settings.projects_root
    if not root.is_dir():
        return Check(
            "projects root",
            False,
            f"{root} does not exist — a task with no project starts in "
            f"{settings.data_dir / 'workspace'}; set {env_var_name('projects_root')}",
            severity="soft",
            section="projects",
            unset=True,
        )
    return Check("projects root", True, str(root), severity="soft", section="projects")


# --- plugins ---------------------------------------------------------------------


#: What turning each plugin on does not reach while it is off, when its secret is set anyway.
_UNUSED_SECRET = {
    "send_to_slack": "PIN-lockout alerts are not going to Slack",
    "check_billing": "nothing reads it",
    "check_email": "questions about your email are dispatched as tasks instead",
}


def _secret_without_plugin(settings: Settings, name: str) -> str | None:
    """The secret (or sign-in) this plugin uses that is there while the plugin is off."""
    if name == "send_to_slack" and settings.slack_bot_token:
        return env_var_name("slack_bot_token")
    if name == "check_billing":
        for field in ("openai_admin_key", "anthropic_admin_key"):
            if getattr(settings, field):
                return env_var_name(field)
    if name == "check_email" and token_path(settings).is_file():
        return "a Gmail sign-in"
    return None


def _plugin_detail(status: plugins.Status) -> str:
    values = status.values or {}
    if status.name == "send_to_slack":
        where = values.get("channel_id") or "the MCP server's channel"
        server = values.get("mcp_server")
        return f"posts to {where}" + (f"; subagents use the {server} MCP server" if server else "")
    if status.name == "check_email":
        return f"{values.get('model')}, {values.get('effort')} effort"
    if status.name == "check_billing":
        budget = values.get("monthly_budget")
        return f"{values.get('provider')}" + (f", budget ${budget:g}" if budget else "")
    guard = values.get("guard") or "the built-in guard"
    return f"{', '.join(values.get('clusters') or {})} through {guard}"


def plugin_checks(settings: Settings) -> list[Check]:
    """One soft check per plugin: does it load. Off is optional, not a failure.

    The same load a call makes (`plugins.status`), so a plugin whose file is refused — not
    signed in, a TOML that does not validate — says why here. A secret set for a plugin
    that is off is the one half-done state worth a warning: somebody meant to use it.
    """
    try:
        statuses = plugins.status(settings)
    except Exception as exc:  # the owner's own files are loaded too; never take doctor down
        return [Check("plugins", False, f"could not load {settings.custom_tools_dir}: {exc}",
                      severity="soft", section="plugins")]
    checks = []
    for status in statuses:
        name = status.name
        if status.on:
            checks.append(Check(name, True, f"on — {_plugin_detail(status)}", severity="soft",
                                section="plugins"))
        elif status.installed:
            checks.append(Check(name, False, f"on but refused: {status.refused}",
                                severity="soft", section="plugins"))
        elif (secret := _secret_without_plugin(settings, name)) is not None:
            checks.append(Check(
                name, False,
                f"{secret} is set but {name} is off, so {_UNUSED_SECRET[name]} — "
                f"`keryx plugins install {name}`",
                severity="soft", section="plugins",
            ))
        else:
            checks.append(Check(
                name, False,
                f"off (optional) — `keryx setup` → Plugins, or `keryx plugins install {name}`",
                severity="soft", section="plugins", unset=True,
            ))
    return checks


def _retired_check(store: ConfigStore) -> Check:
    """Settings a plugin replaced, still in `config.toml`: ignored, and so a plugin that is
    off without its owner knowing. The upgrade path is one command."""
    try:
        leftover = plugins.retired_in(store)
    except Exception:  # a config.toml that does not parse is `_config_check`'s to report
        leftover = {}
    if leftover:
        return Check(
            "retired settings",
            False,
            f"{', '.join(leftover)} are plugin settings now and nothing reads them — "
            "`keryx plugins install --from-settings`",
            severity="soft",
            section="plugins",
        )
    return Check("retired settings", True, "none", severity="soft", section="plugins")


def _issues_check(settings: Settings) -> Check:
    """Whether a bug or feature request said on a call can become an issue: off, or `gh`.

    Soft: without it a report is still written up, into the task's own report. A token in
    the keyring is named rather than failed, because whether a background service can open
    it depends on the desktop, and `gh` says so itself when it cannot.
    """
    name = "issue reports"
    if not settings.issue_reporting:
        return Check(name, True, "off — `keryx setup` turns them on", severity="soft",
                     section="issues")
    checkout = settings.checkout
    if checkout is None or not (checkout / SKILL).is_file():
        return Check(
            name,
            False,
            "no Keryx checkout with the skill in it — set KERYX_CHECKOUT to a clone",
            severity="soft",
            section="issues",
            unset=True,
        )
    status = gh_status()
    if not status.installed:
        detail = f"gh is not installed ({GH_INSTALL_URL}); until it is, reports are not filed"
        return Check(name, False, detail, severity="soft", section="issues", unset=True)
    if not status.signed_in:
        return Check(name, False, "gh is not signed in to GitHub — `gh auth login`",
                     severity="soft", section="issues", unset=True)
    detail = f"filed on {settings.issue_repo}" + (
        f" as {status.account}" if status.account else ""
    )
    if status.keyring:
        detail += (
            "; gh keeps its token in the keyring, which the background service may not open "
            "(`gh auth login --insecure-storage` keeps it in a private file instead)"
        )
    return Check(name, True, detail, severity="soft", section="issues")
