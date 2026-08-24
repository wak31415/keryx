"""The checks behind `jarvis doctor`: is this machine actually able to run Jarvis?

`run_doctor_checks` is a pure function over `Settings` (plus the filesystem and `PATH`)
returning a list of `Check`s, so the CLI is left with printing and an exit code. Hardware
and heavy imports (`sounddevice`, `openwakeword`) are reached through the small `_query_*`
helpers below — guarded imports inside functions, swappable in tests, never at module
scope.

Severity: a `hard` failure means Jarvis will not work and `doctor` exits non-zero; a
`soft` one is a warning (no mic on this machine, no PIN, no Google credentials) that
merely narrows what Jarvis can do.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jarvis.config import PLACEHOLDER_KEY, Settings

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


def run_doctor_checks(settings: Settings, *, probe_mic: bool = True) -> list[Check]:
    """Every check, in the order they are printed. Never raises: problems come back as checks."""
    checks = [
        _env_file_check(),
        _openai_key_check(settings),
        _subagent_auth_check(settings),
        _claude_cli_check(),
        _twilio_check(settings),
        _secret_check(
            "PUBLIC_HOST",
            settings.public_host,
            "Twilio cannot reach this machine",
            reveal=True,
        ),
        _tunnel_check(),
        _allowed_callers_check(settings),
        _pin_check(settings),
        _wakeword_check(settings),
    ]
    if probe_mic:
        checks.append(_microphone_check())
    checks.append(_data_dir_check(settings))
    checks.append(_google_check(settings))
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


def _has_claude_subscription_login() -> bool:
    """Best-effort: does the Claude CLI have a stored subscription login on this machine?"""
    if (Path.home() / ".claude" / ".credentials.json").exists():
        return True
    try:  # macOS stores the login in the Keychain instead of a file
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


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
    return Check("Twilio credentials", True, str(settings.twilio_number))


def _allowed_callers_check(settings: Settings) -> Check:
    """Without an allowlist every inbound call is refused."""
    if not settings.allowed_callers:
        return Check("allowed callers", False, "ALLOWED_CALLERS is empty — every call is refused")
    return Check("allowed callers", True, ", ".join(settings.allowed_callers))


def _pin_check(settings: Settings) -> Check:
    """Warn-only: no PIN just means no destructive work from the phone."""
    if not settings.pin:
        return Check("PIN", False, "not set — coding/cowork refused on phone", severity="soft")
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
            "openwakeword is not installed — the wake word needs macOS; serve --no-wakeword",
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


def _data_dir_check(settings: Settings) -> Check:
    """Can we actually write tasks, transcripts and logs where they are meant to go?"""
    path = settings.data_dir
    probe = path / WRITE_PROBE_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check("data dir writable", False, f"{path}: {exc}")
    return Check("data dir writable", True, str(path))


def _google_check(settings: Settings) -> Check:
    """Warn-only: cowork tasks need credentials from `jarvis setup-google`."""
    if not settings.google_oauth_client():
        return Check(
            "Google credentials",
            True,
            "not configured — cowork tasks are unavailable",
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
