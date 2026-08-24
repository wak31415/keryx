"""Application settings, loaded from environment variables / `.env`."""

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Annotated

from pydantic import Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

log = logging.getLogger("jarvis.config")

#: Stand-in for a missing `OPENAI_API_KEY`, so read-only commands (`jarvis tasks`,
#: `jarvis doctor`, `jarvis download-models`) can still load settings on a half-configured
#: machine. `doctor` recognises it and reports the key as unset.
PLACEHOLDER_KEY = "unset"

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
    "pin",
    "public_host",
    "report_secret",
    "google_oauth_client_id",
    "google_oauth_client_secret",
    "user_google_email",
)


class Settings(BaseSettings):
    """Jarvis runtime configuration. See spec §3.4 for the env-var table."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, env_ignore_empty=True
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

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = Field(default=None, repr=False)
    twilio_number: str | None = None

    # Access control
    allowed_callers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    owner_number_explicit: str | None = Field(default=None, validation_alias="OWNER_NUMBER")
    pin: str | None = Field(default=None, validation_alias="JARVIS_PIN", repr=False)

    # Networking
    public_host: str | None = None
    host: str = "127.0.0.1"
    port: int = 8080

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
    daily_task_cap: int = 50

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

    # Logging
    log_level: str = "INFO"

    # Test/dev escape hatches (not in the spec §3.4 table)
    debug_skip_twilio_validation: bool = False
    fake_agents: bool = False

    @field_validator("allowed_callers", mode="before")
    @classmethod
    def _parse_allowed_callers(cls, value: object) -> object:
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

    @field_validator(
        "data_dir", "projects_root", "skills_dir", "google_client_secrets_file", mode="after"
    )
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        return value.expanduser()

    def google_oauth_client(self) -> tuple[str, str] | None:
        """The OAuth client as `(id, secret)`: the env pair if set, else the JSON file.

        The console hands out that file with the pair nested under `installed` (desktop
        clients) or `web`; either shape is accepted. A missing or malformed file simply
        means "no Google", which the doctor reports and cowork tasks refuse.
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
    def owner_number(self) -> str | None:
        """Explicit `OWNER_NUMBER`, else the first allowed caller, else None."""
        if self.owner_number_explicit:
            return self.owner_number_explicit
        if self.allowed_callers:
            return self.allowed_callers[0]
        return None

    def ensure_dirs(self) -> None:
        """Create `data_dir` and its `tasks`/`calls` subdirectories."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "tasks").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "calls").mkdir(parents=True, exist_ok=True)

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

        self.data_dir.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_hex(32)
        secret_path.write_text(secret)
        os.chmod(secret_path, 0o600)
        return secret


def load_settings(**overrides: object) -> Settings:
    """Build a `Settings` instance, optionally overriding fields (used by tests/CLI)."""
    return Settings(**overrides)
