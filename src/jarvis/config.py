"""Application settings, loaded from environment variables / `.env`."""

import os
import secrets
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Stand-in for a missing `OPENAI_API_KEY`, so read-only commands (`jarvis tasks`,
#: `jarvis doctor`, `jarvis download-models`) can still load settings on a half-configured
#: machine. `doctor` recognises it and reports the key as unset.
PLACEHOLDER_KEY = "unset"

#: Every optional string setting. `.env.example` ships them blank (`JARVIS_PIN=`), and a
#: blank one means *not configured*, never the empty string — an empty PIN would otherwise
#: be a PIN that `submit_pin("")` matches (spec §3.3).
OPTIONAL_STR_FIELDS = (
    "anthropic_api_key",
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

    # OpenAI Realtime
    openai_api_key: str
    openai_realtime_model: str = "gpt-realtime-2.1"
    openai_voice: str = "marin"
    openai_transcription_model: str = "gpt-4o-mini-transcribe"

    # Claude Agent SDK
    anthropic_api_key: str | None = None
    subagent_model: str = "claude-opus-5"
    subagent_max_turns: int = 200
    subagent_max_budget_usd: float = 10.0

    # Twilio
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_number: str | None = None

    # Access control
    allowed_callers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    owner_number_explicit: str | None = Field(default=None, validation_alias="OWNER_NUMBER")
    pin: str | None = Field(default=None, validation_alias="JARVIS_PIN")

    # Networking
    public_host: str | None = None
    host: str = "127.0.0.1"
    port: int = 8080

    # Projects
    projects: dict[str, str] = Field(default_factory=dict)
    projects_root: Path = Path("~/Local/coding_projects")

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
    report_secret: str | None = None

    # Google integration
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    user_google_email: str | None = None

    # Logging
    log_level: str = "INFO"

    # Test/dev escape hatches (not in spec §3.4 table, needed by later tasks)
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

    @field_validator("data_dir", "projects_root", mode="after")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        return value.expanduser()

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
        """The configured report secret, or a persisted random one at `data_dir/report_secret`."""
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
