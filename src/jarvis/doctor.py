"""The checks behind `jarvis doctor`: is this machine actually able to run Jarvis?

`run_doctor_checks` is a pure function over `Settings` (plus the filesystem and `PATH`)
returning a list of `Check`s, so the CLI is left with printing and an exit code. Hardware
and heavy imports (`sounddevice`, `openwakeword`) are reached through the small `_query_*`
helpers below — guarded imports inside functions, swappable in tests, never at module
scope.

Severity: a `hard` failure means Jarvis will not work and `doctor` exits non-zero; a
`soft` one is a warning (no mic on this machine, no PIN, no Google credentials) that
merely narrows what Jarvis can do. The last few checks are about what Jarvis knows and
offers rather than whether it runs — the owner's name, the memory, the projects root, and
whether `cluster_stats` and `send_to_slack` are offered — so somebody who did not write it
can find out why a tool is missing without reading the source.
"""

import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jarvis.agents.claude import claude_stored_login as _has_claude_subscription_login
from jarvis.config import (
    DATA_DIR_MODE,
    OWNER_FALLBACK,
    PLACEHOLDER_KEY,
    Settings,
    env_var_name,
)
from jarvis.continuity.memory import memory_path, read_memory
from jarvis.integrations import slack
from jarvis.logging_util import mask_number
from jarvis.restart.service import INSTALLERS, candidate_target, resolve_target

Severity = Literal["hard", "soft"]

ENV_FILE = ".env"

#: The file `doctor` writes and deletes to prove `data_dir` is writable.
WRITE_PROBE_NAME = ".doctor-write-probe"

MARKERS = {"ok": "✅", "hard": "❌", "soft": "⚠️"}

CLAUDE_CLI_HINT = "the Agent SDK needs it — npm i -g @anthropic-ai/claude-code"
#: Tunnels that can put `/twilio/*` in front of Twilio, best first. The deployment uses
#: Cloudflare Tunnel; ngrok still counts, so a dev machine set up before the move passes.
TUNNEL_BINARIES = ("cloudflared", "ngrok")
#: Where `claude-agent-sdk` 0.2.x keeps the CLI it ships with, relative to the package.
BUNDLED_CLI_PATH = ("_bundled", "claude")


@dataclass(frozen=True)
class Check:
    """One diagnostic: what was looked at, whether it is fine, and what was found."""

    name: str
    ok: bool
    detail: str = ""
    severity: Severity = "hard"


def format_check(check: Check) -> str:
    """One printable line: a marker, the check's name, and the detail behind it."""
    marker = MARKERS["ok"] if check.ok else MARKERS[check.severity]
    return f"{marker}  {check.name}: {check.detail}" if check.detail else f"{marker}  {check.name}"


def has_hard_failure(checks: list[Check]) -> bool:
    """True when something failed that stops Jarvis from working at all."""
    return any(not check.ok and check.severity == "hard" for check in checks)


def run_doctor_checks(
    settings: Settings,
    *,
    probe_mic: bool = True,
    config_problems: Mapping[str, str] | None = None,
) -> list[Check]:
    """Every check, in the order they are printed. Never raises: problems come back as checks.

    `config_problems` maps a `Settings` field name to why its configured value was refused,
    for the fields the CLI had to replace to load at all (see `cli.DOCTOR_FALLBACKS`).
    Without it a rejected value is indistinguishable from an unset one.
    """
    problems = config_problems or {}
    checks = [
        _env_file_check(),
        _openai_key_check(settings),
        _subagent_auth_check(settings),
        _claude_cli_check(),
        _twilio_check(settings),
        _signature_check(settings),
        _secret_check(
            "PUBLIC_HOST",
            settings.public_host,
            "Twilio cannot reach this machine",
            reveal=True,
        ),
        _tunnel_check(),
        _allowed_callers_check(settings),
        _pin_check(settings, problems.get("pin")),
        _wakeword_check(settings),
    ]
    if probe_mic:
        checks.append(_microphone_check())
    checks.append(_service_manager_check(settings))
    checks.append(_data_dir_check(settings))
    checks.append(_data_dir_privacy_check(settings))
    checks.append(_google_check(settings))
    checks.append(_owner_name_check(settings))
    checks.append(_memory_check(settings))
    checks.append(_projects_root_check(settings))
    checks.append(_cluster_check(settings))
    checks.append(_slack_check(settings))
    return checks


# --- configuration ---------------------------------------------------------


def _env_file_check() -> Check:
    """`.env` in the current directory — where every other setting comes from."""
    path = Path(ENV_FILE)
    if not path.is_file():
        return Check(ENV_FILE, False, f"no {ENV_FILE} in {Path.cwd()} — copy .env.example to .env")
    return Check(ENV_FILE, True, str(path.resolve()))


def _openai_key_check(settings: Settings) -> Check:
    """The one required key; `PLACEHOLDER_KEY` means the read-only loader filled it in."""
    key = settings.openai_api_key
    if not key or key == PLACEHOLDER_KEY:
        return Check("OPENAI_API_KEY", False, "not set — the voice session cannot start")
    return Check("OPENAI_API_KEY", True, "set")


def _secret_check(name: str, value: str | None, consequence: str, *, reveal: bool = False) -> Check:
    """A plain "is this configured" check whose failure detail says what breaks.

    `reveal` prints the value back (hostnames are worth seeing; keys are not).
    """
    if not value:
        return Check(name, False, f"not set — {consequence}")
    return Check(name, True, value if reveal else "set")


def _subagent_auth_check(settings: Settings) -> Check:
    """One of three ways subagents can authenticate; soft-fails because the login
    detection is a heuristic (an odd Keychain setup could hide a working login)."""
    if settings.anthropic_api_key:
        return Check("subagent auth", True, "ANTHROPIC_API_KEY set (pay-per-token)")
    if settings.claude_code_oauth_token:
        return Check("subagent auth", True, "CLAUDE_CODE_OAUTH_TOKEN set (subscription)")
    if _has_claude_subscription_login():
        return Check("subagent auth", True, "Claude CLI subscription login (default)")
    return Check(
        "subagent auth",
        False,
        "no login found — run `claude /login` once, or `claude setup-token` for headless,"
        " or set ANTHROPIC_API_KEY",
        severity="soft",
    )


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
            "Twilio credentials", False, f"missing {', '.join(missing)} — no phone channel"
        )
    return Check("Twilio credentials", True, mask_number(settings.twilio_number))


def _signature_check(settings: Settings) -> Check:
    """Is every Twilio webhook signature-checked? Off behind a tunnel, `serve` refuses.

    Hard when `PUBLIC_HOST` is set, because that is exactly the configuration `jarvis serve`
    will not start in; a warning without one, where it is the local-development switch it
    was meant to be.
    """
    if not settings.debug_skip_twilio_validation:
        return Check("Twilio signatures", True, "validated")
    refusal = settings.phone_refusal()
    if refusal is not None:
        return Check("Twilio signatures", False, refusal)
    return Check(
        "Twilio signatures",
        False,
        "DEBUG_SKIP_TWILIO_VALIDATION is on — for a machine nothing outside can reach",
        severity="soft",
    )


def _allowed_callers_check(settings: Settings) -> Check:
    """Without an allowlist every inbound call is refused."""
    if not settings.allowed_callers:
        return Check("allowed callers", False, "ALLOWED_CALLERS is empty — every call is refused")
    return Check("allowed callers", True, ", ".join(map(mask_number, settings.allowed_callers)))


def _pin_check(settings: Settings, problem: str | None = None) -> Check:
    """No PIN is a warning; a PIN that does not conform is a machine that will not start.

    The two are different failures. Unset means every dispatch is refused from the phone —
    limited, but safe, and a deliberate way to run. Set-but-malformed means `jarvis serve`
    raises on load, so this has to be hard, and it has to say what is wrong: `doctor` is
    the command whose whole job is to be runnable when nothing else is.
    """
    if problem is not None:
        return Check("PIN", False, f"{env_var_name('pin')} is set but unusable — {problem}")
    if not settings.pin:
        return Check("PIN", False, "not set — every task is refused on the phone", severity="soft")
    return Check("PIN", True, "set", severity="soft")


# --- the machine -----------------------------------------------------------


def _bundled_claude_cli() -> Path | None:
    """The `claude` binary shipped inside `claude_agent_sdk`, if this install has one."""
    try:
        import claude_agent_sdk
    except Exception:  # pragma: no cover - the SDK is a hard dependency
        return None
    path = Path(claude_agent_sdk.__file__).parent.joinpath(*BUNDLED_CLI_PATH)
    return path if path.is_file() else None


def _claude_cli_check() -> Check:
    """The CLI the Agent SDK drives: the one it bundles, else one on `PATH`.

    Soft: the SDK looks for its bundled binary first, so a machine without either is a
    warning about subagents, not a reason to call the whole install broken.
    """
    bundled = _bundled_claude_cli()
    if bundled is not None:
        return Check("claude CLI", True, f"bundled with claude-agent-sdk: {bundled}")
    found = shutil.which("claude")
    if not found:
        return Check(
            "claude CLI",
            False,
            f"not bundled, not on PATH — {CLAUDE_CLI_HINT}",
            severity="soft",
        )
    return Check("claude CLI", True, found)


def _tunnel_check() -> Check:
    """Is a tunnel binary on `PATH`? Without one Twilio cannot reach this machine."""
    for name in TUNNEL_BINARIES:
        found = shutil.which(name)
        if found:
            return Check("tunnel", True, found)
    return Check(
        "tunnel",
        False,
        f"none of {', '.join(TUNNEL_BINARIES)} on PATH — no tunnel for the phone channel",
    )


def _wakeword_models_dir() -> Path:
    """Where openWakeWord keeps its downloaded `.onnx` models. Raises if it isn't installed."""
    import openwakeword

    return Path(openwakeword.__file__).parent / "resources" / "models"


def _wakeword_check(settings: Settings) -> Check:
    """Has `jarvis download-models` been run for the configured wake word?"""
    model = settings.wakeword_model
    try:
        models_dir = _wakeword_models_dir()
        found = sorted(models_dir.glob(f"{model}*.onnx"))
    except ImportError:
        # openwakeword is a macOS-only dependency (see pyproject): on a Linux host the
        # wake-word channel is simply absent, which narrows Jarvis rather than breaking it.
        return Check(
            "wake-word model",
            False,
            "openwakeword is not installed — the wake word needs macOS, so `jarvis serve` "
            "runs the phone channel alone",
            severity="soft",
        )
    except Exception as exc:
        return Check("wake-word model", False, f"openwakeword is unusable: {exc}")
    if not found:
        return Check(
            "wake-word model",
            False,
            f"no {model}*.onnx in {models_dir} — run `jarvis download-models`",
        )
    return Check("wake-word model", True, ", ".join(path.name for path in found))


def _query_input_device() -> Any:
    """The default input device, via sounddevice. Raises when there is no mic (or no PortAudio)."""
    import sounddevice

    return sounddevice.query_devices(kind="input")


def _microphone_check() -> Check:
    """Warn-only: a machine with no mic can still take phone calls."""
    try:
        device = _query_input_device()
    except Exception as exc:
        return Check("microphone", False, f"no input device: {exc}", severity="soft")
    name = device.get("name") if isinstance(device, dict) else str(device)
    return Check("microphone", True, str(name), severity="soft")


def _service_manager_check(settings: Settings) -> Check:
    """Whether a service is installed to supervise Jarvis, and what is lost when none is.

    Warn-only: running without one — `SERVICE_MANAGER=none`, no `systemctl`/`launchctl`,
    or simply no unit installed — is a supported way to run Jarvis, just a narrower one.
    It is worth saying out loud because the consequence is silent: `jarvis restart` and the
    voice model's `restart_service` both refuse, so a subagent that changes Jarvis's own
    code has no way to make the change take effect.

    Asked from outside, like `jarvis restart`: `doctor` runs in a terminal, never inside
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
            f"{why} — `jarvis restart` and the voice's restart_service refuse, and nothing "
            "restarts Jarvis if it dies",
            severity="soft",
        )
    detail = target.describe()
    if shutil.which("git") is None:
        # `current_version` is decoration and degrades quietly; say so once, here, rather
        # than leave someone wondering why every restart reports an unknown version.
        detail += " — but no git on PATH, so versions will report as unknown"
    return Check("service manager", True, detail, severity="soft")


def _data_dir_check(settings: Settings) -> Check:
    """Can we actually write tasks, transcripts and logs where they are meant to go?"""
    path = settings.data_dir
    probe = path / WRITE_PROBE_NAME
    try:
        # `mode=` rather than `secure_dir`: `doctor` must not leave a world-readable
        # `~/.jarvis` behind on a machine that did not have one, and it must not quietly
        # tighten one that does — the next check's job is to report what is actually there.
        path.mkdir(mode=DATA_DIR_MODE, parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("data dir writable", False, f"{path}: {exc}")
    return Check("data dir writable", True, str(path))


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
        return Check("data dir private", False, f"{path}: {exc}", severity="soft")

    exposed = mode & (stat.S_IRWXG | stat.S_IRWXO)
    if exposed:
        return Check(
            "data dir private",
            False,
            f"{path} is {mode:04o} — transcripts and reports are readable by others; "
            f"chmod {DATA_DIR_MODE:04o} it",
            severity="soft",
        )
    return Check("data dir private", True, f"{mode:04o} (owner only)", severity="soft")


def _google_check(settings: Settings) -> Check:
    """Warn-only, and only about `workspace-mcp` — which is off unless asked for.

    With it off, Gmail and Calendar reach the subagents through the Claude CLI's own
    claude.ai connectors, which need nothing from us.
    """
    if not settings.google_workspace_mcp:
        return Check(
            "Google credentials",
            True,
            "workspace-mcp is off — Gmail and Calendar come from the Claude connectors",
            severity="soft",
        )
    if not settings.google_oauth_client():
        return Check(
            "Google credentials",
            True,
            "not configured — Gmail and Calendar are unavailable to subagents",
            severity="soft",
        )
    credentials_dir = settings.data_dir / "google"
    stored = sorted(credentials_dir.glob("*")) if credentials_dir.is_dir() else []
    if not stored:
        return Check(
            "Google credentials",
            False,
            f"nothing in {credentials_dir} — run `jarvis setup-google`",
            severity="soft",
        )
    return Check("Google credentials", True, str(credentials_dir), severity="soft")


# --- what Jarvis knows, and what it offers ---------------------------------


def _owner_name_check(settings: Settings) -> Check:
    """Warn-only: without a name the prompts say `OWNER_FALLBACK`, which still works."""
    if not settings.owner_name:
        return Check(
            "owner name",
            False,
            f'{env_var_name("owner_name")} is not set — the prompts call you "{OWNER_FALLBACK}"',
            severity="soft",
        )
    return Check("owner name", True, settings.owner_label, severity="soft")


def _memory_check(settings: Settings) -> Check:
    """Warn-only: a Jarvis with no memory opens every call knowing nothing about its owner.

    Read, never created: `doctor` reports what is there.
    """
    memory = read_memory(settings.data_dir)
    if not memory:
        return Check(
            "memory",
            False,
            "nothing remembered yet — `jarvis init` writes a first memory, and every "
            "authorized call adds to it",
            severity="soft",
        )
    path = memory_path(settings.data_dir)
    return Check(
        "memory", True, f"{len(memory)} characters in {path}, sent on every call", severity="soft"
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
        )
    return Check("projects root", True, str(root), severity="soft")


def _cluster_check(settings: Settings) -> Check:
    """Is `cluster_stats` offered, and if half of it is configured, which half is missing.

    The same condition as `integrations.cluster.build_cluster_stats`. Nothing configured is
    a tick, not a warning: the tool is a worked example most machines have no use for.
    """
    clusters = env_var_name("clusters")
    guard_name = env_var_name("cluster_ssh_guard")
    guard = settings.cluster_ssh_guard
    if not settings.clusters and guard is None:
        return Check(
            "cluster stats",
            True,
            f"not configured — cluster_stats is not offered ({clusters}, {guard_name})",
            severity="soft",
        )
    if not settings.clusters:
        return Check(
            "cluster stats",
            False,
            f"{guard_name} is set but {clusters} is empty — cluster_stats is not offered",
            severity="soft",
        )
    if guard is None or not guard.is_file():
        where = "is not set" if guard is None else f"is not a file at {guard}"
        return Check(
            "cluster stats",
            False,
            f"{clusters} is set but {guard_name} {where} — cluster_stats is not offered",
            severity="soft",
        )
    return Check(
        "cluster stats",
        True,
        f"cluster_stats is offered for {', '.join(settings.clusters)}",
        severity="soft",
    )


def _slack_check(settings: Settings) -> Check:
    """Is `send_to_slack` offered, and are subagents told about a Slack server.

    The same resolution the application uses (`integrations.slack.slack_credentials`), so
    a route that only exists in the named MCP server's own config counts. Nothing configured
    is a tick, not a warning; half a route is a warning, because somebody meant to set it.
    """
    token, channel = env_var_name("slack_bot_token"), env_var_name("slack_channel_id")
    server = settings.slack_mcp_server
    if not (settings.slack_bot_token or settings.slack_channel_id or server):
        return Check(
            "Slack",
            True,
            f"not configured — send_to_slack is not offered ({token}, {channel} or "
            f"{env_var_name('slack_mcp_server')})",
            severity="soft",
        )
    route = slack.slack_credentials(
        settings.slack_bot_token,
        settings.slack_channel_id,
        server=server,
        config_path=slack.CLAUDE_CONFIG,
    )
    subagents = f"; subagents use the {server} MCP server" if server else ""
    if route is None:
        missing = (
            f"{server} has no bot token and channel in {slack.CLAUDE_CONFIG}"
            if server
            else f"{token} and {channel} are both needed"
        )
        return Check(
            "Slack",
            False,
            f"{missing} — send_to_slack is not offered{subagents}",
            severity="soft",
        )
    return Check("Slack", True, f"send_to_slack is offered{subagents}", severity="soft")
