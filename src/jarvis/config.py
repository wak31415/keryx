"""Application settings, loaded from environment variables / `.env`."""

import contextlib
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

log = logging.getLogger("jarvis.config")

#: Stand-in for a missing `OPENAI_API_KEY`, so read-only commands (`jarvis tasks`,
#: `jarvis doctor`, `jarvis download-models`) can still load settings on a half-configured
#: machine. `doctor` recognises it and reports the key as unset.
PLACEHOLDER_KEY = "unset"

#: What Jarvis calls its owner when `OWNER_NAME` is blank. It reads as a role rather than a
#: name on purpose: a prompt that says "you are the owner's assistant" is still true, where
#: a made-up name would have the model greet a stranger by it.
OWNER_FALLBACK = "the owner"

#: Every optional string setting. `.env.example` ships them blank (`JARVIS_PIN=`), and a
#: blank one means *not configured*, never the empty string — an empty PIN would otherwise
#: be a PIN that `submit_pin("")` matches (spec §3.3).
OPTIONAL_STR_FIELDS = (
    "anthropic_api_key",
    "claude_code_oauth_token",
    "twilio_account_sid",
    "twilio_auth_token",
    "twilio_number",
    "owner_number_explicit",
    "owner_name",
    "pin",
    "public_host",
    "report_secret",
    "google_oauth_client_id",
    "google_oauth_client_secret",
    "user_google_email",
    "slack_bot_token",
    "slack_channel_id",
    "slack_mcp_server",
    "service_unit",
    "approval_quiet_hours",
    "openai_admin_key",
    "openai_billing_project_id",
    "openai_billing_api_key_id",
    "anthropic_admin_key",
    "anthropic_billing_workspace_id",
)


#: What a configured PIN has to be. Digits, because it is keyed into a phone: a PIN with
#: a letter in it cannot be entered at all today, so accepting one only ever produced a
#: caller who could not authorize. Six of them at minimum because this is the single thing
#: between someone who has spoofed a caller ID and a subagent running with
#: `bypassPermissions`, and eight at most because it is said or keyed under time pressure.
PIN_PATTERN = re.compile(r"\d{6,8}")
PIN_RULE = "must be 6 to 8 digits, and nothing but digits"

#: What a cluster name or partition has to be. Both are handed to a remote shell, so
#: anything but a bare word is refused when the settings load rather than on a call.
CLUSTER_WORD = re.compile(r"[A-Za-z0-9_.-]+")


#: Modes for everything under `data_dir`. Owner-only, both of them, because of what is
#: actually in there: `calls/*.log` is every word of every call, `tasks.db` and
#: `tasks/*.md` are what was asked for and what came back, and `memory.md` is what Jarvis
#: knows about its owner between calls. The default umask on most machines is 022, which
#: makes all of that world-readable to anyone else with an account.
DATA_DIR_MODE = 0o700
DATA_FILE_MODE = 0o600


def secure_dir(path: Path) -> Path:
    """Create a directory under `data_dir`, owner-only, tightening one that already exists."""
    path.mkdir(parents=True, exist_ok=True)
    # Best effort: a mode that cannot be set (a mounted share, another owner) is not a
    # reason to refuse to run, and `jarvis doctor` reports the result either way.
    with contextlib.suppress(OSError):
        os.chmod(path, DATA_DIR_MODE)
    return path


def secure_file(path: Path) -> Path:
    """Tighten a file under `data_dir` to owner-only. A file that is not there is fine."""
    with contextlib.suppress(OSError):
        os.chmod(path, DATA_FILE_MODE)
    return path


class Settings(BaseSettings):
    """Jarvis runtime configuration. See spec §3.4 for the env-var table."""

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
        env_ignore_empty=True,
        # Almost every field here is a credential, and pydantic quotes the rejected input
        # back in the error it raises. A `JARVIS_PIN` that fails the rule below must not
        # end up in a traceback, a journal or a terminal on the way to being fixed.
        hide_input_in_errors=True,
    )

    #: Cached `report_secret_value()`: it is asked for per notification and per report
    #: request, and the fallback lives in a file.
    _report_secret_cache: str | None = PrivateAttr(default=None)

    # OpenAI Realtime
    openai_api_key: str = Field(repr=False)
    openai_realtime_model: str = "gpt-realtime-2.1"
    openai_voice: str = "cedar"
    openai_transcription_model: str = "gpt-4o-mini-transcribe"
    #: Answers the voice model's own `web_search` tool, through the Responses API (the
    #: Realtime API has no hosted search tool of its own).
    openai_web_search_model: str = "gpt-5.4-mini"

    # Claude Agent SDK. Subagent auth, in order of precedence: ANTHROPIC_API_KEY
    # (pay-per-token) > CLAUDE_CODE_OAUTH_TOKEN (subscription, headless; from
    # `claude setup-token`) > the Claude CLI's stored subscription login (default).
    anthropic_api_key: str | None = Field(default=None, repr=False)
    claude_code_oauth_token: str | None = Field(default=None, repr=False)
    subagent_model: str = "claude-opus-5"
    subagent_max_turns: int = 200
    subagent_max_budget_usd: float = 10.0

    # Billing (jarvis/integrations/billing.py, behind the voice model's `check_billing`). Read-only,
    # and on an *admin*-scoped credential: the key the voice agent talks to the model with
    # cannot read `/v1/organization/costs`, so a separate one is named here. Left unset,
    # billing falls back to the ordinary key above and reports the 401 it gets, which is a
    # clearer answer than pretending the tool does not exist.
    #: Which account to report on. "auto" is OpenAI — the key this very call runs on.
    billing_provider: Literal["auto", "openai", "anthropic"] = "auto"
    openai_admin_key: str | None = Field(default=None, repr=False)
    #: Narrows the spend figure to one project. Costs cannot be narrowed any finer than
    #: this: the endpoint takes `project_ids` and has no per-key filter.
    openai_billing_project_id: str | None = None
    #: Narrows *token usage* (not spend) to the one key, if you know its `key_…` id.
    openai_billing_api_key_id: str | None = None
    anthropic_admin_key: str | None = Field(default=None, repr=False)
    anthropic_billing_workspace_id: str | None = None
    #: What he considers a month's budget, in the provider's currency. Neither provider
    #: serves a spend limit over the API, so the percentage is only as real as this number.
    billing_monthly_budget: float | None = None

    # Cluster stats (jarvis/integrations/cluster.py, behind the voice model's `cluster_stats`).
    # A worked example, off until both settings below are set and the guard is on disk:
    # read-only Slurm reads, routed through an ssh guard script you supply. The guard is the
    # whole point: where cluster auth is 2FA behind an ssh ControlMaster, a direct
    # connection against a dead master hangs rather than failing, and a retry storm of those
    # is how an address gets banned by the login nodes.
    #: The guard script (its contract is in `jarvis/integrations/cluster.py`). Blank: no tool.
    cluster_ssh_guard: Path | None = None
    #: The clusters it may ask about, as `{"name": "partition"}`: the name is the ssh host
    #: alias the guard is handed and the word the model says, the partition is where the
    #: GPUs are. Empty: no tool. Nothing else the model says ever reaches the guard.
    clusters: dict[str, str] = Field(default_factory=dict)
    #: How long one cluster may take to answer. They are all queried at once, so this is the
    #: whole wait — and it is a wait inside a phone call.
    cluster_query_timeout_s: float = 20.0

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = Field(default=None, repr=False)
    twilio_number: str | None = None
    #: Whether Jarvis may text at all. Off by default: many Twilio accounts lack SMS
    #: permission for their region, where every send fails with an HTTP 400, and written
    #: messages go to Slack when asked for. Outbound *calls* are unaffected, which matters:
    #: the restart watchdog's alert is a call, and it is the only thing that still works
    #: when Jarvis itself is down.
    sms_enabled: bool = False

    # The owner
    #: What Jarvis calls the person it works for, in the prompts it is handed. Blank is
    #: `OWNER_FALLBACK`. Only a name: everything else Jarvis knows about them is the memory.
    owner_name: str | None = None

    # Access control
    allowed_callers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    owner_number_explicit: str | None = Field(default=None, validation_alias="OWNER_NUMBER")
    #: Six to eight digits when set, enforced below. Unset stays legal and means "no PIN":
    #: every dispatch is refused from the phone.
    pin: str | None = Field(default=None, validation_alias="JARVIS_PIN", repr=False)

    #: Wrong PINs, counted across every call, before PIN entry locks (jarvis/pin_guard.py).
    #: The per-call limit of three ends a call; this is what ends a guessing campaign. Ten is
    #: three failed calls and then some — more than he mistypes in a day — and one guess in
    #: a hundred thousand of a 6-digit PIN.
    pin_failure_limit: int = Field(default=10, ge=1)
    #: How long a wrong PIN is remembered. Past the limit, every further wrong PIN inside
    #: this window locks entry again, so a campaign gets one guess per lockout from then on.
    pin_failure_window_hours: float = Field(default=24, gt=0)
    #: How long PIN entry stays locked, for everyone and even for the right PIN. An hour is
    #: the longest his own fumbling can cost him, and holds a campaign to ~24 guesses a day.
    pin_lockout_minutes: float = Field(default=60, gt=0)

    # Networking
    public_host: str | None = None
    host: str = "127.0.0.1"
    port: int = 8080

    # The service manager `jarvis restart` (and the voice's `restart_service`) asks to
    # restart this process. "auto" is systemd on Linux, launchd on macOS, and nothing at
    # all when neither is on PATH — a Jarvis started by hand has nothing to bring it back,
    # so it refuses to stop rather than take itself off the air.
    service_manager: Literal["auto", "systemd", "launchd", "none"] = "auto"
    #: The unit (systemd) or label (launchd) to restart; blank means the installed default.
    service_unit: str | None = None

    # Projects
    projects: dict[str, str] = Field(default_factory=dict)
    projects_root: Path = Path("~/Local/coding_projects")
    #: Where the Claude CLI keeps its skills; listed in the voice prompt so the model
    #: knows what the subagents are good at without being told.
    skills_dir: Path = Path("~/.claude/skills")

    # Data storage
    data_dir: Path = Path("~/.jarvis")

    # Task concurrency / limits
    max_concurrent_tasks: int = 3
    dispatch_wait_max_seconds: int = 25
    local_silence_timeout: float = 30  # seconds; 0 disables the local silence timeout
    max_call_seconds: float = 1800  # seconds; 0 disables the phone call-duration limit
    #: Phone sessions open at once, each of them a realtime session being paid for. Two, so
    #: a dropped call can be redialled while the old session is still saying its goodbye;
    #: a call past it is told the line is busy and never reaches the model.
    max_phone_sessions: int = Field(default=2, ge=1)
    daily_task_cap: int = 50

    # Turn detection: how long Jarvis waits before deciding you have finished speaking.
    # "semantic" waits on whether the sentence sounds finished (so a pause to think does
    # not cut you off); "server" is a plain silence timer of `vad_silence_ms`.
    vad_mode: Literal["server", "semantic"] = "semantic"
    #: Only read in semantic mode, where there is no timer to set: "low" waits longest,
    #: "high" jumps in soonest, "auto" is "medium". Was "low" until 2026-08-26, which left
    #: about two seconds of silence at the end of every sentence — long enough to sound
    #: like it had not heard. "medium" lands near a second and still waits out a pause.
    vad_eagerness: Literal["low", "medium", "high", "auto"] = "medium"
    vad_silence_ms: int = 1200
    vad_threshold: float = 0.5
    vad_prefix_ms: int = 300
    #: Background-noise suppression on what the model hears. "auto" picks by channel —
    #: `near_field` for a phone held to the head, `far_field` for the Mac's microphone
    #: across the room — which is what `noise_reduction_for` resolves. Mostly this is
    #: about barge-in: noise the model mistakes for speech is noise that cuts it off.
    noise_reduction: Literal["auto", "near_field", "far_field", "off"] = "auto"

    # Wake word
    wakeword_model: str = "hey_jarvis"
    wakeword_threshold: float = 0.5

    # Report links
    report_secret: str | None = Field(default=None, repr=False)

    # Google integration
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = Field(default=None, repr=False)
    user_google_email: str | None = None
    #: A Google "OAuth client" JSON (the file the cloud console hands you). Read when the
    #: id/secret pair above is unset, so the secret can stay in a file instead of the env.
    google_client_secrets_file: Path = Path(".secrets/client_secret.json")
    #: Attach the `workspace-mcp` stdio server to every subagent. Off since 2026-08-24:
    #: the Claude CLI already carries authorized claude.ai Gmail/Calendar/Drive connectors,
    #: so this only added an unauthorized second path for a subagent to trip over. The
    #: wiring is kept for a machine whose subagents authenticate with an API key instead,
    #: where those connectors do not exist.
    google_workspace_mcp: bool = False

    # Slack. The voice model's `send_to_slack` needs a bot token and a DM channel; the
    # subagents need a Slack MCP server of their own in the Claude CLI.
    slack_bot_token: str | None = Field(default=None, repr=False)
    slack_channel_id: str | None = None
    #: The user-scope MCP server in `~/.claude.json` that gives subagents Slack. Named, the
    #: subagent prompt tells them to use it when asked, and the token and channel above
    #: fall back to that server's config. Blank: no fallback, and subagents are told nothing
    #: about Slack.
    slack_mcp_server: str | None = None

    # The approval bridge (jarvis/approvals): a Claude Code prompt he never answered
    # becomes a phone call. Off makes the socket never bind, which is exactly what the
    # hook finds on a machine that has not opted in — it exits and the prompt stays put.
    approvals_enabled: bool = True
    #: How long a prompt has to sit on his screen unanswered before Jarvis rings about it.
    approval_escalate_seconds: float = 300
    #: How long after that the hook keeps waiting for an answer from the call. The two
    #: added together are the longest the hook can block, so the `timeout` on the hook
    #: entry in `~/.claude/settings.json` has to be comfortably larger than the sum.
    approval_call_window_seconds: float = 240
    #: How many approval calls may go out in an hour, however many prompts pile up. Alert
    #: fatigue is the real failure mode: a bridge that rings ten times a day gets muted,
    #: and then it is not there for the one that mattered.
    approval_max_per_hour: int = 4
    #: `HH:MM-HH:MM` in which it never rings (may cross midnight); blank is never. Local
    #: time as the *host* sees it — see the note in `approvals/broker.py::_quiet_now`.
    approval_quiet_hours: str | None = None
    #: The only shell commands a keypad digit may ever run, each matched word for word;
    #: only `git push` and `git commit` may carry arguments, and only the few their rules
    #: in jarvis/approvals/policy.py name. No test runner: a test run executes whatever the
    #: session last wrote into the working tree, and the read-back cannot say so.
    approval_bash_allow: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["git push", "git commit"]
    )
    #: Where a file may be written by phone approval; blank means the projects root plus
    #: every explicitly configured project.
    approval_roots: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Retention (jarvis/continuity/retention.py). Both are **off** at 0, which is what Jarvis has
    # always done: nothing is deleted, ever. A default that deleted a man's own call
    # transcripts because nobody changed a number is not a default worth having, so the
    # feature exists and the policy is his. See also `jarvis forget`.
    transcript_retention_days: int = 0
    task_retention_days: int = 0

    # Logging
    log_level: str = "INFO"

    # Test/dev escape hatches (not in the spec §3.4 table)
    debug_skip_twilio_validation: bool = False
    fake_agents: bool = False

    @field_validator("allowed_callers", "approval_bash_allow", "approval_roots", mode="before")
    @classmethod
    def _parse_comma_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(*OPTIONAL_STR_FIELDS, mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """A blank value is "not set" — `env_ignore_empty` for anything passed by hand."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("transcript_retention_days", "task_retention_days", mode="after")
    @classmethod
    def _retention_is_not_negative(cls, value: int) -> int:
        """0 is off. A negative window would be a cutoff in the future — delete everything."""
        if value < 0:
            raise ValueError("must be 0 (keep everything) or a positive number of days")
        return value

    @field_validator("pin", mode="after")
    @classmethod
    def _pin_is_six_to_eight_digits(cls, value: str | None) -> str | None:
        """A configured PIN must conform, or Jarvis does not start (spec §5).

        Blank is handled upstream by `_blank_is_unset` and stays "no PIN", which is a
        different and safe thing: it refuses every dispatch from the phone. What this
        refuses is a PIN that is *set* and not worth having.
        """
        if value is None or PIN_PATTERN.fullmatch(value):
            return value
        raise ValueError(PIN_RULE)

    @field_validator("cluster_ssh_guard", mode="before")
    @classmethod
    def _blank_guard_is_unset(cls, value: object) -> object:
        """A blank path is no guard, not the current directory."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("clusters", mode="after")
    @classmethod
    def _clusters_are_bare_words(cls, value: dict[str, str]) -> dict[str, str]:
        """Names lower-cased (they are matched against speech), and both halves bare words."""
        clusters: dict[str, str] = {}
        for name, partition in value.items():
            name, partition = name.strip().lower(), partition.strip()
            if not (CLUSTER_WORD.fullmatch(name) and CLUSTER_WORD.fullmatch(partition)):
                raise ValueError("each cluster name and partition must be a bare word")
            clusters[name] = partition
        return clusters

    @field_validator(
        "data_dir",
        "projects_root",
        "skills_dir",
        "google_client_secrets_file",
        "cluster_ssh_guard",
        mode="after",
    )
    @classmethod
    def _expand_path(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    def google_oauth_client(self) -> tuple[str, str] | None:
        """The OAuth client as `(id, secret)`: the env pair if set, else the JSON file.

        The console hands out that file with the pair nested under `installed` (desktop
        clients) or `web`; either shape is accepted. A missing or malformed file simply
        means "no Google", which the doctor reports and Gmail/Calendar work refuses.
        """
        if self.google_oauth_client_id and self.google_oauth_client_secret:
            return self.google_oauth_client_id, self.google_oauth_client_secret

        path = self.google_client_secrets_file
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read the Google client secrets at %s: %s", path, exc)
            return None
        block = data.get("installed") or data.get("web") or data
        client_id, client_secret = block.get("client_id"), block.get("client_secret")
        if not (client_id and client_secret):
            log.warning("%s has no client_id/client_secret pair", path)
            return None
        return client_id, client_secret

    @property
    def owner_label(self) -> str:
        """`OWNER_NAME` as the prompts say it, else `OWNER_FALLBACK`."""
        return self.owner_name.strip() if self.owner_name else OWNER_FALLBACK

    @property
    def owner_number(self) -> str | None:
        """Explicit `OWNER_NUMBER`, else the first allowed caller, else None."""
        if self.owner_number_explicit:
            return self.owner_number_explicit
        if self.allowed_callers:
            return self.allowed_callers[0]
        return None

    def phone_refusal(self) -> str | None:
        """Why the phone server must not start as configured, in one line; None if it may.

        `DEBUG_SKIP_TWILIO_VALIDATION` is for a machine nothing outside can reach. With a
        `PUBLIC_HOST` set there is a tunnel pointing at it, and with the signature check off
        anyone who can reach that tunnel can pose as Twilio — mint stream tokens, open media
        sockets and key PINs in at machine speed.
        """
        if self.debug_skip_twilio_validation and self.public_host:
            return (
                "DEBUG_SKIP_TWILIO_VALIDATION is on while PUBLIC_HOST is set, so anyone who "
                "can reach the tunnel could pose as Twilio — turn it off, or serve --no-phone"
            )
        return None

    def noise_reduction_for(self, channel: str) -> Literal["near_field", "far_field"] | None:
        """The noise-reduction profile for `channel`, or None to leave it off."""
        if self.noise_reduction == "off":
            return None
        if self.noise_reduction != "auto":
            return self.noise_reduction
        return "near_field" if channel == "phone" else "far_field"

    def ensure_dirs(self) -> None:
        """Create `data_dir` and its `tasks`/`calls`/`approvals` subdirectories, owner-only.

        Existing directories are tightened in place, so an install made before this simply
        becomes private the next time anything starts. `approvals/` was already 0700 for
        its socket; the rest of `data_dir` holds transcripts and reports and deserved the
        same from the start.
        """
        secure_dir(self.data_dir)
        for name in ("tasks", "calls", "approvals"):
            secure_dir(self.data_dir / name)

    def report_secret_value(self) -> str:
        """The configured report secret, or a persisted random one at `data_dir/report_secret`.

        Read once and kept: every notification and every report request asks for it.
        """
        if self._report_secret_cache is None:
            self._report_secret_cache = self._load_report_secret()
        return self._report_secret_cache

    def _load_report_secret(self) -> str:
        if self.report_secret:
            return self.report_secret

        secret_path = self.data_dir / "report_secret"
        if secret_path.exists():
            return secret_path.read_text().strip()

        secure_dir(self.data_dir)
        secret = secrets.token_hex(32)
        secret_path.write_text(secret)
        secure_file(secret_path)
        return secret


def env_var_name(field_name: str) -> str:
    """The environment variable a `Settings` field is read from.

    Its validation alias where it has one (`pin` is `JARVIS_PIN`), else the field name
    upcased. Used to report a configuration problem in the name its owner set it under.
    """
    field = Settings.model_fields.get(field_name)
    alias = getattr(field, "validation_alias", None) if field is not None else None
    return alias if isinstance(alias, str) else field_name.upper()


def load_settings(**overrides: object) -> Settings:
    """Build a `Settings` instance, optionally overriding fields (used by tests/CLI)."""
    return Settings(**overrides)
