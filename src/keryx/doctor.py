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
import re
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from keryx import fixes, plugins
from keryx.agents.registry import (
    BACKENDS,
    auth_status,
    cli_path,
    install_command,
    installed,
    without_google,
)
from keryx.agents.registry import harness as agent_harness
from keryx.child_env import redact_known
from keryx.config import (
    DATA_DIR_MODE,
    DATA_FILE_MODE,
    OWNER_FALLBACK,
    PIN_FROM_ENV,
    PIN_FROM_FILE,
    Settings,
    env_var_name,
    field_for,
    field_title,
    pin_file,
)
from keryx.config.files import HOME_ENV, LEGACY_HOME_ENV
from keryx.config.permissions import is_protected
from keryx.config.settings import LEGACY_ENV_FILE
from keryx.config.store import ConfigStore
from keryx.continuity.memory import memory_path, read_memory
from keryx.endpoints import Endpoint, Probe, probe, warnings
from keryx.integrations.gmail import token_path
from keryx.issues import GH_INSTALL_URL, SKILL, gh_status
from keryx.localmodels import runtimes, servers
from keryx.localmodels.server import OLLAMA_URL
from keryx.logging_util import mask_number
from keryx.realtime.openai import realtime_problem
from keryx.restart.service import INSTALLERS, candidate_target, resolve_target

Severity = Literal["hard", "soft"]

#: The file `doctor` writes and deletes to prove `data_dir` is writable.
WRITE_PROBE_NAME = ".doctor-write-probe"

MARKERS = {"ok": "✅", "hard": "❌", "soft": "⚠️", "optional": "○ "}

#: Tunnels that can put `/twilio/*` in front of Twilio, best first. The deployment uses
#: Cloudflare Tunnel; ngrok still counts, so a dev machine set up before the move passes.
TUNNEL_BINARIES = ("cloudflared", "ngrok")

#: What gives a terminal's text away — a command, a prompt, a traceback — which nothing the
#: Mac app shows may hold (`app_facing`).
SHELL_TEXT = ("`", "keryx ", "claude ", "brew ", "uv ", "scripts/", "$ ", "/login",
              "Traceback")
#: A path on this machine: a word that starts at the root, or at home.
_PATH = re.compile(r"(?:^|[\s(\[\"'])~?/(?!/)\S")

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
    #: Not fine because what it asks did not answer this time (Twilio unreachable): it says
    #: nothing either way about the setup, so it holds no section or setup step back.
    unknown: bool = False
    #: What a terminal adds to `detail`: the command that mends it, or where the thing is on
    #: disk. `detail` is written for a person at the Mac app — no command, no path, no
    #: traceback — and `keryx doctor` prints the two together (`text`).
    hint: str | None = None
    #: What the Mac app can do about it, as one of `keryx.fixes`' ids; None when it can do
    #: nothing, or nothing is wrong.
    fix: str | None = None

    @property
    def state(self) -> str:
        """`ok`, `missing` or `failed` — what `--json` and the wizard read."""
        return "ok" if self.ok else ("missing" if self.unset else "failed")

    @property
    def text(self) -> str:
        """The detail with the terminal's hint after it: what `keryx doctor` prints."""
        if self.hint and self.detail:
            return f"{self.detail} — {self.hint}"
        return self.detail or self.hint or ""

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "state": self.state, "title": check_title(self.name)}


#: Check names too terse to read beside the settings' titles, as the Overview shows them.
CHECK_TITLES = {
    "data dir writable": "Data folder writable",
    "data dir private": "Data folder private",
    "outside git": "Settings outside Git",
    "config.toml": "Settings file",
    "old .env": "Old .env file",
}


def app_facing(text: str) -> bool:
    """Whether `text` is fit for the Mac app: no command, no traceback, no path. What a
    check's `detail` and the setup plan's `why` must be; a terminal's goes in `hint`."""
    return not any(shell in text for shell in SHELL_TEXT) and not _PATH.search(text)


def check_title(name: str) -> str:
    """What `--json` calls a check where a person reads it (the Mac app's Overview).

    In order: a check named by a setting's key (`OPENAI_API_KEY`, or an older name for one)
    takes that setting's title, and one named by a plugin the plugin's; then
    `CHECK_TITLES`; any other is its name with the first letter capitalised, unless it holds
    a `.` or a `_` — a file or an identifier, left as written. Only a key in capitals counts
    as a setting's, so a check that happened to be called `port` would not be one.
    """
    field = field_for(name) if name == name.upper() else None
    if field is not None:
        return field_title(field)
    if name in plugins.PLUGINS:
        return plugins.PLUGINS[name].title
    if name in CHECK_TITLES:
        return CHECK_TITLES[name]
    if "." in name or "_" in name:
        return name
    return name[:1].upper() + name[1:]


@dataclass(frozen=True)
class EndpointReach:
    """The two reads `doctor` makes of a model server, over the network: the model list, and
    a Realtime session opened and closed. Only `keryx doctor` hands one in; the wizard, which
    reads these checks on every screen, stays off the network."""

    models: Callable[[Endpoint], Probe] = probe
    realtime: Callable[[Endpoint], str | None] = realtime_problem


def format_check(check: Check) -> str:
    """One printable line: a marker, the check's name, and the detail and hint behind it."""
    if check.ok:
        marker = MARKERS["ok"]
    elif check.unset and check.severity == "soft":
        marker = MARKERS["optional"]
    else:
        marker = MARKERS[check.severity]
    return f"{marker}  {check.name}: {check.text}" if check.text else f"{marker}  {check.name}"


def has_hard_failure(checks: list[Check]) -> bool:
    """True when something failed that stops Keryx from working at all."""
    return any(not check.ok and check.severity == "hard" for check in checks)


def run_doctor_checks(
    settings: Settings,
    *,
    config_problems: Mapping[str, str] | None = None,
    store: ConfigStore | None = None,
    twilio: Any | None = None,
    reach: EndpointReach | None = None,
) -> list[Check]:
    """Every check, in the order they are printed. Never raises: problems come back as checks.

    `config_problems` maps a `Settings` field name to why its configured value was refused,
    for the fields the CLI had to replace to load at all (see `cli.DOCTOR_FALLBACKS`).
    Without it a rejected value is indistinguishable from an unset one. `twilio` is a
    `notify.twilio_out.TwilioAdmin`; without one the webhook is not looked at, and without
    `reach` no model server is asked anything.
    """
    problems = config_problems or {}
    store = store or ConfigStore()
    checks = [
        _storage_check(settings),
        _config_check(store),
        *_voice_checks(settings),
        _agent_config_check(settings),
        *_agent_checks(settings),
        _local_overview_check(settings),
        *_local_server_checks(settings),
        *endpoint_checks(settings, reach),
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
        google_check(settings),
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
    if refusal is None:
        return Check("storage", True, "the XDG directories", section="import")
    if refusal.startswith(f"{LEGACY_HOME_ENV}="):  # the environment's: nothing to move
        return Check("storage", False, f"{LEGACY_HOME_ENV} is set and {HOME_ENV} is not, so "
                     "Keryx would not find its settings", section="import", unset=True,
                     hint=refusal)
    if "no longer reads" in refusal:  # a `.env` or Google client file in the working dir
        detail = "settings in an old .env file have not been moved into Keryx's store yet"
    else:
        detail = "files from when Keryx was called Jarvis have not been moved yet"
    return Check("storage", False, detail, section="import", unset=True, hint=refusal,
                 fix="migrate")


def _config_check(store: ConfigStore) -> Check:
    """Where the configuration lives."""
    if store.config_path.is_file():
        return Check("configuration", True, "saved", severity="soft", section="import",
                     hint=str(store.config_path))
    return Check("configuration", True, "nothing saved yet", severity="soft", section="import",
                 hint=str(store.home))


def _voice_checks(settings: Settings) -> list[Check]:
    """Where a call's voice goes, and the OpenAI key it may or may not need.

    With `VOICE_BASE_URL` blank the key is the voice, and Keryx cannot take a call without
    it. With a voice server of the owner's own it buys only the voice model's `web_search`,
    so its absence is a warning.
    """
    key = settings.openai_key
    if not settings.voice_base_url:
        if key is None:
            return [
                Check(
                    "OPENAI_API_KEY",
                    False,
                    "not set — the voice session cannot start",
                    section="voice",
                    unset=True,
                    fix=fixes.secret("OPENAI_API_KEY"),
                )
            ]
        return [Check("OPENAI_API_KEY", True, "set", section="voice")]
    checks = [Check("voice", True, f"your own server at {settings.voice_base_url}",
                    section="voice")]
    if key is None:
        checks.append(Check("OPENAI_API_KEY", False, "not set — calls have no web_search",
                            severity="soft", section="voice", unset=True,
                            fix=fixes.secret("OPENAI_API_KEY")))
    else:
        checks.append(Check("OPENAI_API_KEY", True, "set, for web_search", severity="soft",
                            section="voice"))
    return checks


def _agent_config_check(settings: Settings) -> Check:
    """`AGENT_BACKEND` among `AGENTS_ENABLED`, which `keryx serve` refuses to start without."""
    refusal = settings.agent_refusal()
    if refusal is not None:
        detail, _, hint = refusal.partition(" — ")
        if settings.agents_enabled and settings.agent_backend not in settings.agents_enabled:
            fix = fixes.setting("AGENTS_ENABLED")
        elif installed(settings.agent_backend, settings):
            fix = "models:choose"  # the local agent with no server: choosing a model sets one
        else:
            fix = fixes.setting("AGENT_BACKEND")  # another agent; installing one is a terminal's
        return Check("coding agents", False, detail, section="agents", hint=hint or None,
                     fix=fix)
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
    `keryx auth status --smoke` is the test that actually runs one. The `local` agent's
    CLI is its harness's, and its "credential" is the address of its server.
    """
    checks = []
    for name in settings.enabled_agents:
        spec = BACKENDS[name]
        severity: Severity = "hard" if name == settings.agent_backend else "soft"
        label = f"{spec.label} agent" + (" (default)" if name == settings.agent_backend else "")
        section = "local" if spec.harness is not None else "agents"
        if not installed(name, settings):
            hint = install_command(name, settings) if spec.harness else spec.install_hint
            checks.append(
                Check(
                    label,
                    False,
                    "not installed",
                    severity=severity,
                    section=section,
                    unset=True,
                    hint=hint,
                )
            )
            continue
        cli = cli_path(name, settings)
        harness = BACKENDS[agent_harness(name, settings)]
        if cli is None:
            checks.append(Check(label, False, f"the {harness.name} program is missing",
                                severity=severity, section=section, hint=harness.install_hint))
            continue
        status = auth_status(name, settings)
        # A credential that is not there says so, then what to run: the terminal's half.
        login = status.detail.partition(" — ")[2] or status.detail
        version = harness.cli_version()
        where = f"{cli} ({version})" if version else cli
        if spec.harness is not None:
            # Its "credential" is its server's address, which is no secret and no path.
            checks.append(Check(label, status.ready,
                                f"in {harness.label}; {status.detail}" if status.ready
                                else "no server set", severity=severity, section=section,
                                unset=not status.ready,
                                hint=None if status.ready else login,
                                fix=None if status.ready else "models:choose"))
            continue
        checks.append(
            Check(
                label,
                status.ready,
                status.detail if status.ready else "not signed in",
                severity=severity,
                section=section,
                unset=not status.ready,
                hint=where if status.ready else login,
                fix=None if status.ready else fixes.signin(name),
            )
        )
    return checks


def _local_overview_check(settings: Settings) -> Check:
    """Optional, and what the Local models section is for: is anything on your own hardware?"""
    parts = []
    if settings.voice_base_url:
        parts.append(f"voice at {settings.voice_base_url}")
    if settings.local_agent_endpoint is not None:
        parts.append(f"agent {settings.local_agent_model or '?'} at "
                     f"{settings.local_agent_endpoint.base_url}")
    if not parts:
        return Check("local models", True, "none (optional) — the voice and the agent can run "
                     "on your own hardware", severity="soft", section="local",
                     hint="`keryx setup` sets them up")
    return Check("local models", True, "; ".join(parts), severity="soft", section="local")


def _local_server_checks(settings: Settings) -> list[Check]:
    """The servers `keryx setup` runs here: the model on disk and the programs installed."""
    checks = []
    if settings.llm_server_model:
        severity: Severity = "hard" if settings.agent_backend == "local" else "soft"
        try:
            alias, path, _context = servers.llm_model(settings)
        except servers.ServerConfigError as error:
            checks.append(Check("model server", False, "the chosen model is not on disk",
                                severity=severity, section="local", hint=str(error),
                                fix="models:choose"))
        else:
            if runtimes.llama_server(settings.cache_dir) is None:
                checks.append(Check("model server", False, "llama-server is not installed",
                                    severity=severity, section="local",
                                    hint="`keryx models start` or `keryx setup` installs it",
                                    fix="models:start"))
            else:
                checks.append(Check("model server", True, f"{alias}, on disk",
                                    severity=severity, section="local", hint=str(path)))
    if settings.voice_base_url == _own_voice(settings):
        found = runtimes.speech_to_speech()
        if found is None:
            checks.append(Check("voice server program", False,
                                "speech-to-speech is not installed", section="local",
                                hint="`keryx voice use here` or `keryx setup` installs it",
                                fix="voice:here"))
        else:
            checks.append(Check("voice server program", True, "speech-to-speech is installed",
                                section="local", hint=found))
    return checks


def endpoint_checks(settings: Settings, reach: EndpointReach | None = None) -> list[Check]:
    """A voice server and a local agent's server: where they are, and, given `reach`,
    whether they answer.

    Where each is only ever warns (`endpoints.warnings`): a public address with no key may
    be behind a firewall nobody can see from here. Whether it answers is hard for whatever
    a call or the default agent cannot do without.
    """
    checks: list[Check] = []
    if settings.voice_base_url:
        voice = settings.voice_endpoint
        checks += [
            Check("voice server", False, warning, severity="soft", section="local",
                  fix=_warning_fix(warning, "VOICE_BASE_URL", "VOICE_API_KEY"))
            for warning in warnings(voice, carries_voice=True)
        ]
        if reach is not None:
            problem = reach.realtime(voice)
            if problem is None:
                checks.append(Check("voice server", True,
                                    f"{voice.base_url} opens a Realtime session",
                                    section="local"))
            else:
                # Its own words go to the terminal: a server's error can be a traceback.
                here = settings.voice_base_url == _own_voice(settings)
                checks.append(Check("voice server", False,
                                    f"{voice.host} did not open a Realtime session",
                                    section="local", hint=problem,
                                    fix="voice:here" if here
                                    else fixes.setting("VOICE_BASE_URL")))
    agent = settings.local_agent_endpoint
    if agent is None:
        return checks
    severity: Severity = "hard" if settings.agent_backend == "local" else "soft"
    name = "local agent server"
    checks += [
        Check(name, False, warning, severity="soft", section="local",
              fix=_warning_fix(warning, "LOCAL_AGENT_BASE_URL", "LOCAL_AGENT_API_KEY"))
        for warning in warnings(agent)
    ]
    # A server Keryx runs here is started from the app; one elsewhere is only an address.
    ours = agent.base_url in (f"http://{servers.HOST}:{settings.llm_server_port}/v1",
                              f"{OLLAMA_URL}/v1")
    model_fix = "models:choose" if ours else fixes.setting("LOCAL_AGENT_MODEL")
    if reach is None:
        if not agent.model:
            checks.append(Check(name, False, "LOCAL_AGENT_MODEL is not set",
                                severity=severity, section="local", unset=True, fix=model_fix))
        return checks
    found = reach.models(agent)
    listed = ", ".join(found.models[:6]) + (", …" if len(found.models) > 6 else "")
    if not found.ok:
        checks.append(Check(name, False, found.problem or "no answer", severity=severity,
                            section="local", fix="models:start" if ours
                            else fixes.setting("LOCAL_AGENT_BASE_URL")))
    elif not agent.model:
        checks.append(Check(name, False, f"LOCAL_AGENT_MODEL is not set — it serves {listed}",
                            severity=severity, section="local", unset=True, fix=model_fix))
    elif found.models and agent.model not in found.models:
        checks.append(Check(name, False, f"{agent.model} is not one it serves ({listed})",
                            severity=severity, section="local",
                            fix="models:start" if ours else model_fix))
    else:
        checks.append(Check(name, True, f"{agent.base_url} serves {agent.model}",
                            severity=severity, section="local"))
    return checks


def _own_voice(settings: Settings) -> str:
    """The address of the voice server Keryx runs on this machine."""
    return f"http://{servers.HOST}:{settings.voice_server_port}/v1"


def _warning_fix(warning: str, address: str, key: str) -> str:
    """What mends a warning about where a server is: its key, when it has none, else its
    address."""
    return fixes.secret(key) if "no key is set" in warning else fixes.setting(address)


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
            fix="phone:account",
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
    fix = fixes.setting("DEBUG_SKIP_TWILIO_VALIDATION")
    refusal = settings.phone_refusal()
    if refusal is not None:
        return Check("Twilio signatures", False, refusal, section="phone", fix=fix)
    return Check(
        "Twilio signatures",
        False,
        "DEBUG_SKIP_TWILIO_VALIDATION is on — for a machine nothing outside can reach",
        severity="soft",
        section="phone",
        fix=fix,
    )


def _public_host_check(settings: Settings) -> Check:
    if not settings.public_host:
        return Check(
            "PUBLIC_HOST",
            False,
            "not set — Twilio cannot reach this machine",
            section="phone",
            unset=True,
            fix="phone:tunnel",
        )
    return Check("PUBLIC_HOST", True, settings.public_host, section="phone")


def _webhook_check(settings: Settings, twilio: Any) -> Check:
    """Is the number pointed at this machine? Read-only, and soft: Twilio may be down.

    Pointed here is both webhooks, the voice URL and the status callback, compared as
    `keryx phone webhooks` compares them (`phone.matches`), so doctor, `setup plan` and the
    phone screen agree. Only asked when there is a number and a host to compare; `keryx
    setup` is what sets it, after asking.
    """
    # Late: `keryx.phone` reads this module.
    from keryx.phone import (
        BAD_HOST,
        hostname_problem,
        matches,
        refused_credentials,
        twilio_problem,
        webhook_urls,
    )

    if not (settings.twilio_number and settings.public_host):
        return Check("Twilio webhook", True, "not checked — no number or PUBLIC_HOST yet",
                     severity="soft", section="phone")
    if hostname_problem(settings.public_host):
        return Check("Twilio webhook", False, BAD_HOST, severity="soft", section="phone",
                     fix="phone:tunnel")
    want, want_status = webhook_urls(settings.public_host)
    try:
        numbers = {number.phone_number: number for number in twilio.numbers()}
    except Exception as exc:  # a warning, never a crash; by its cause: its text may quote the token
        if refused_credentials(exc):  # an answer: these credentials do not work
            return Check("Twilio webhook", False, twilio_problem(exc), severity="soft",
                         section="phone", fix="phone:account")
        return Check("Twilio webhook", False, f"could not ask Twilio: {twilio_problem(exc)}",
                     severity="soft", section="phone", unknown=True)
    number = numbers.get(settings.twilio_number)
    if number is None:
        return Check(
            "Twilio webhook",
            False,
            f"{mask_number(settings.twilio_number)} is not a number on this Twilio account",
            section="phone",
            fix="phone:account",
        )
    if not matches(number, settings.public_host):
        if (number.voice_url or "").rstrip("/") != want:
            found = f"calls go to {number.voice_url or 'nothing'}, not {want}"
        else:
            found = f"call status goes to {number.status_callback or 'nothing'}, not {want_status}"
        return Check(
            "Twilio webhook",
            False,
            found,
            severity="soft",
            section="phone",
            hint="`keryx setup` can point it here",
            fix="phone:webhooks",
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
            fix=fixes.setting("ALLOWED_CALLERS"),
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
            f"set on {_enrolled_on(path)}",
            severity="soft",
            section="owner",
            hint=f"kept in {path}",
        )
    if settings.pin_enrolment_open:
        return Check(
            "PIN",
            False,
            "no PIN yet — the first call can set one, and nothing of yours is read out "
            "until then",
            severity="soft",
            section="owner",
            unset=True,
            hint="`keryx setup` sets one now",
            fix="pin",
        )
    return Check(
        "PIN",
        False,
        "the PIN file is not 6-8 digits, so there is no PIN and no call can set one",
        severity="soft",
        section="owner",
        hint=f"{path}; `keryx setup` replaces it",
        fix="pin",
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
            fix=fixes.setting(env_var_name("owner_name")),
        )
    return Check("owner name", True, settings.owner_label, severity="soft", section="owner")


# --- the machine -----------------------------------------------------------


def _tunnel_check() -> Check:
    """Is a tunnel binary on `PATH`? Without one Twilio cannot reach this machine."""
    for name in TUNNEL_BINARIES:
        found = shutil.which(name)
        if found:
            return Check("tunnel", True, f"{name} is installed", section="phone", hint=found)
    return Check(
        "tunnel",
        False,
        f"the tunnel program, {TUNNEL_BINARIES[0]}, is not installed — Twilio cannot reach "
        "this machine without one",
        section="phone",
        unset=True,
        hint=f"none of {', '.join(TUNNEL_BINARIES)} on PATH; `brew install cloudflared`",
        fix="phone:tunnel",
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
        refused = "`keryx restart` and the voice's restart_service refuse"
        fix: str | None = None
        if settings.service_manager == "none":
            why, hint = "SERVICE_MANAGER is none", refused
            fix = fixes.setting("SERVICE_MANAGER")
        elif candidate is None:
            why, hint = "no service manager on this machine", refused
        else:
            why = f"{candidate.unit} is not installed"
            hint = f"{INSTALLERS[candidate.manager]} installs it; until then {refused}"
            fix = "service:install"
        return Check(
            "background process",
            False,
            f"{why} — Keryx cannot restart itself, and nothing restarts it if it dies",
            severity="soft",
            section="service",
            unset=True,
            hint=hint,
            fix=fix,
        )
    detail = target.describe()
    if shutil.which("git") is None:
        # `current_version` is decoration and degrades quietly; say so once, here, rather
        # than leave someone wondering why every restart reports an unknown version.
        detail += " — but no git on PATH, so versions will report as unknown"
    return Check("background process", True, detail, severity="soft", section="service")


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
        return Check("data dir writable", False, "Keryx cannot write to its data folder",
                     section="security", hint=f"{path}: {exc}")
    return Check("data dir writable", True, "yes", section="security", hint=str(path))


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
        return Check("data dir private", False, "Keryx cannot look at its data folder",
                     severity="soft", section="security", hint=f"{path}: {exc}")

    exposed = mode & (stat.S_IRWXG | stat.S_IRWXO)
    if exposed:
        return Check(
            "data dir private",
            False,
            f"the data folder is {mode:04o} — transcripts and reports are readable by others",
            severity="soft",
            section="security",
            hint=f"{path}; `keryx doctor --fix` makes it owner-only",
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
        count = f"{len(loose)} file{'s' * (len(loose) != 1)} or folder{'s' * (len(loose) != 1)}"
        return Check(
            "secret files private",
            False,
            f"{count} holding secrets can be read by others",
            severity="soft",
            section="security",
            hint=f"{names}; `keryx doctor --fix` makes them owner-only",
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
                "Keryx's settings or data are inside a git work tree, one commit from being "
                "pushed somewhere",
                severity="soft",
                section="security",
                hint=f"{path} is inside the git work tree at {tree} — move it (KERYX_HOME, "
                "DATA_DIR) somewhere a commit cannot reach",
            )
    return Check("outside git", True, "not in a git work tree", severity="soft",
                 section="security")


def _secrets_in_config_check(store: ConfigStore) -> Check:
    try:
        found = store.secrets_in_config()
    except Exception as exc:  # a config.toml that does not parse: `serve` will not start
        return Check("config.toml", False, "the settings file does not parse, and only an "
                     "edit by hand can mend it", section="security",
                     hint=f"{redact_known(str(exc))}; fix it by hand")
    if found:
        return Check(
            "config.toml",
            False,
            f"holds {', '.join(found)}, which belong in secrets.toml",
            severity="soft",
            section="security",
            hint=f"`keryx config set {found[0]} --stdin` moves one",
            fix=_secret_fix(found[0]),
        )
    return Check("config.toml", True, "no secrets in it", severity="soft", section="security")


def _secret_fix(key: str) -> str | None:
    """Saving a secret again, which puts it where it belongs; None for a key that is none."""
    fix = f"secret:{key}"
    return fix if fixes.is_valid(fix) else None


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
            "but somebody tried",
            severity="soft",
            section="security",
            hint="remove it from [service_writable]",
        )
    return Check("protected settings", True, "none unlocked", severity="soft",
                 section="security")


def _imported_env_check() -> Check:
    leftovers = sorted(Path.cwd().glob(f"{LEGACY_ENV_FILE.name}.imported-*"))
    if leftovers:
        return Check(
            "old .env",
            False,
            f"{len(leftovers)} old .env "
            f"{'copy still holds' if len(leftovers) == 1 else 'copies still hold'} the secrets "
            "that were imported — delete them once Keryx runs from the store",
            severity="soft",
            section="security",
            hint=", ".join(str(path) for path in leftovers),
        )
    return Check("old .env", True, "none left behind", severity="soft", section="security")


# --- google, and what Keryx knows -----------------------------------------------


def google_check(settings: Settings) -> Check:
    """Optional, and only about `workspace-mcp` — agents sending mail and using the calendar.

    With it off, Gmail and Calendar reach Claude's subagents through the Claude CLI's own
    claude.ai connectors, which need nothing from us. Codex has no connectors, so with Codex
    enabled, off means its tasks have no mailbox and no calendar at all.
    """
    if not settings.google_workspace_mcp:
        if without := without_google(settings):
            return Check(
                "Google for agents",
                False,
                f"not set up (optional) — {' and '.join(without)} tasks have no Gmail or "
                "Calendar",
                severity="soft",
                section="google",
                unset=True,
                hint="`keryx setup` connects Google",
                fix=fixes.integration("google"),
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
            "GOOGLE_WORKSPACE_MCP is on but there is no OAuth client",
            severity="soft",
            section="google",
            hint="`keryx setup` saves one",
            fix=fixes.integration("google"),
        )
    credentials_dir = settings.data_dir / "google"
    stored = sorted(credentials_dir.glob("*")) if credentials_dir.is_dir() else []
    if not stored:
        return Check(
            "Google for agents",
            False,
            "not signed in to Google",
            severity="soft",
            section="google",
            unset=True,
            hint=f"nothing in {credentials_dir} — `keryx auth login google-workspace`",
            fix=fixes.signin("google-workspace"),
        )
    return Check("Google for agents", True, "signed in", severity="soft", section="google",
                 hint=str(credentials_dir))


def _memory_check(settings: Settings) -> Check:
    """Warn-only: a Keryx with no memory opens every call knowing nothing about its owner.

    Read, never created: `doctor` reports what is there.
    """
    memory = read_memory(settings.data_dir)
    if not memory:
        return Check(
            "memory",
            False,
            "nothing remembered yet — every authorized call adds to it",
            severity="soft",
            section="profile",
            unset=True,
            hint="`keryx setup` writes a first memory",
        )
    path = memory_path(settings.data_dir)
    return Check(
        "memory",
        True,
        f"{len(memory)} characters, sent on every call",
        severity="soft",
        section="profile",
        hint=str(path),
    )


def _projects_root_check(settings: Settings) -> Check:
    """Warn-only, and never created: a missing root sends unscoped tasks to the workspace."""
    root = settings.projects_root
    if not root.is_dir():
        key = env_var_name("projects_root")
        return Check(
            "projects root",
            False,
            "the projects folder does not exist — a task with no project starts in Keryx's "
            "own workspace",
            severity="soft",
            section="projects",
            unset=True,
            hint=f"{root}; tasks start in {settings.data_dir / 'workspace'}; set {key}",
            fix=fixes.setting(key),
        )
    return Check("projects root", True, f"the {root.name or 'projects'} folder",
                 severity="soft", section="projects", hint=str(root))


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
    guard = "a guard script of your own" if values.get("guard") else "the built-in guard"
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
        return [Check("plugins", False, "the plugins could not be loaded", severity="soft",
                      section="plugins",
                      hint=f"{settings.custom_tools_dir}: {redact_known(str(exc), settings)}")]
    checks = []
    for status in statuses:
        name = status.name
        if status.on:
            guard = (status.values or {}).get("guard")
            checks.append(Check(name, True, f"on — {_plugin_detail(status)}", severity="soft",
                                section="plugins", hint=str(guard) if guard else None))
        elif status.installed:
            checks.append(_refused_plugin(settings, status))
        elif (secret := _secret_without_plugin(settings, name)) is not None:
            checks.append(Check(
                name, False,
                f"{secret} is set but {name} is off, so {_UNUSED_SECRET[name]}",
                severity="soft", section="plugins",
                hint=f"`keryx plugins install {name}`", fix=fixes.plugin(name),
            ))
        else:
            checks.append(Check(
                name, False, "off (optional)",
                severity="soft", section="plugins", unset=True,
                hint=f"`keryx setup` → Plugins, or `keryx plugins install {name}`",
                fix=fixes.plugin(name),
            ))
    return checks


def _refused_plugin(settings: Settings, status: plugins.Status) -> Check:
    """A plugin that is on and refused: the account it needs when none is connected, which
    is the usual reason (Email with no Gmail sign-in), else that it does not load. The
    loader's own sentence, which can quote a command or a path, is the terminal's."""
    from keryx.integrations.accounts import INTEGRATIONS, connected

    spec = plugins.plugin(status.name)
    hint = f"refused: {redact_known(status.refused, settings)}" if status.refused else None
    if spec.needs and not any(connected(settings, spec.needs).values()):
        account = INTEGRATIONS[spec.needs[0]]
        signin = account.signin if account.signin in fixes.SIGNINS else None
        detail = (f"on, but not signed in to {account.called}" if signin
                  else f"on, but {account.called} is not connected")
        return Check(status.name, False, detail, severity="soft", section="plugins",
                     hint=hint, fix=fixes.signin(signin) if signin
                     else fixes.integration(account.id))
    return Check(status.name, False, "on, but it does not load", severity="soft",
                 section="plugins", hint=hint, fix=fixes.plugin(status.name))


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
            f"{', '.join(leftover)} are plugin settings now and nothing reads them",
            severity="soft",
            section="plugins",
            hint="`keryx plugins install --from-settings` moves them",
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
        return Check(name, True, "off", severity="soft", section="issues",
                     hint="`keryx setup` turns them on")
    checkout = settings.checkout
    if checkout is None or not (checkout / SKILL).is_file():
        return Check(
            name,
            False,
            "there is no Keryx checkout with the report skill in it",
            severity="soft",
            section="issues",
            unset=True,
            hint="set KERYX_CHECKOUT to a clone",
            fix=fixes.setting("KERYX_CHECKOUT"),
        )
    status = gh_status()
    if not status.installed:
        # No fix: the app's GitHub card turns reports on, and installing gh is no setting.
        return Check(name, False, "the GitHub command-line tool, gh, is not installed, so "
                     "reports are not filed", severity="soft", section="issues", unset=True,
                     hint=GH_INSTALL_URL)
    if not status.signed_in:
        return Check(name, False, "gh is not signed in to GitHub", severity="soft",
                     section="issues", unset=True, hint="`gh auth login`")
    # The repository last: its name may end in "keryx", which is no command.
    detail = (f"filed as {status.account} on " if status.account else "filed on ") + (
        settings.issue_repo
    )
    hint = None
    if status.keyring:
        detail += (
            "; gh keeps its token in the keyring, which the background service may not open"
        )
        hint = "`gh auth login --insecure-storage` keeps it in a private file instead"
    return Check(name, True, detail, severity="soft", section="issues", hint=hint)
