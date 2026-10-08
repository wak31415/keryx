"""Application settings: every one of them, where it is read from, and what it is for.

Each field is declared with `setting(...)`, which carries five things beside its type and
default: a `description` (the sentence `keryx config list` prints and
`docs/configuration.md` is generated from), a `title` (the short name a person reads in
place of the environment name: the Mac app's label, in sentence case), a `group` (the
section it is listed and set up under, `GROUPS`, and through it the Mac app's tab, `TABS`,
or the app screen that owns it, `SCREEN_OF_GROUP`), `advanced` (whether the app shows it
under its tab's or screen's "Advanced" disclosure rather than among the key settings), and
`service_writable` (whether the running service may change it by default —
`keryx.config.permissions` has the rest of that rule). A secret is a field
declared `repr=False`; it is stored in `secrets.toml` rather than `config.toml`, and nothing
ever prints it.

A value is taken from the first of these that has one (`settings_customise_sources`):

1. what the code passed in (the CLI's `--port`, a test);
2. the process environment (`KERYX_PIN=… keryx serve`, a systemd `Environment=`);
3. `KERYX_HOME/secrets.toml`, then `KERYX_HOME/config.toml` (`keryx config set`);
4. the default below.

Nothing is read from the working directory. A `.env` there used to be a fifth source, and a
checkout is the one place a secret must never live; `keryx migrate` moves one into the
store, and `storage_refusal` keeps `keryx serve` from starting while one is still there.
"""

import json
import logging
import re
import secrets
import tomllib
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple, get_args

from pydantic import AliasChoices, Field, PrivateAttr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from keryx.config.files import (
    HOME_ENV,
    LEGACY_HOME_ENV,
    claude_config_dir,
    config_file,
    default_cache_dir,
    default_data_dir,
    default_state_dir,
    holds_files,
    keryx_home,
    legacy_entries,
    legacy_home,
    read_toml,
    renamed_dirs,
    secrets_file,
    secure_dir,
    stray_legacy_home_env,
    write_private,
)
from keryx.config.pin import (
    PIN_FROM_ENV,
    PIN_FROM_FILE,
    PIN_PATTERN,
    PIN_RULE,
    pin_file,
    read_enrolled_pin,
    write_enrolled_pin,
)
from keryx.endpoints import LOOPBACK_HOST, OPENAI_BASE_URL, RESPONSES_API, Endpoint
from keryx.persona import DEFAULT_NAME, DEFAULT_VOICE, PERSONAS, voice_for

log = logging.getLogger("keryx.config")

#: Stand-in for a missing `OPENAI_API_KEY`, so read-only commands (`keryx tasks`,
#: `keryx doctor`, `keryx config`) can still load settings on a half-configured
#: machine. `doctor` recognises it and reports the key as unset.
PLACEHOLDER_KEY = "unset"

#: What Keryx calls its owner when `OWNER_NAME` is blank. It reads as a role rather than a
#: name on purpose: a prompt that says "you are the owner's assistant" is still true, where
#: a made-up name would have the model greet a stranger by it.
OWNER_FALLBACK = "the owner"

#: Every optional string setting. A blank one means *not configured*, never the empty
#: string — an empty PIN would otherwise be a PIN that `submit_pin("")` matches.
OPTIONAL_STR_FIELDS = (
    "openai_api_key",
    "voice_base_url",
    "voice_api_key",
    "local_agent_base_url",
    "local_agent_api_key",
    "local_agent_model",
    "agent_server_model",
    "voice_llm_model",
    "voice_llm_base_url",
    "voice_llm_api_key",
    "anthropic_api_key",
    "claude_code_oauth_token",
    "codex_api_key",
    "codex_access_token",
    "codex_model",
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
    "service_unit",
    "approval_quiet_hours",
    "openai_admin_key",
    "anthropic_admin_key",
)

#: The coding agents a task can run on (keryx/agents/registry.py has one entry per name).
#: `local` is a model on a server of the owner's, driven by one of the other two harnesses.
AgentName = Literal["claude", "codex", "local"]
#: Which harness drives the local agent, named by the API its server must speak.
LocalAgentApi = Literal["anthropic-messages", "openai-responses"]
#: Where a call's voice runs: OpenAI Realtime, the speech pipeline on this machine, or a
#: Realtime server elsewhere. A new kind of voice is a new value here, never a flag on one.
VoiceMode = Literal["openai", "here", "server"]
#: Where the pipeline's words come from: the local agent's server, a model server of the
#: voice's own on this machine, or a server elsewhere (a box of the owner's, or a cloud).
VoiceLlmSource = Literal["shared", "here", "remote"]

#: The sections settings are listed and set up under, in the order `keryx setup` and
#: `docs/configuration.md` walk them.
GROUPS: dict[str, str] = {
    "voice": "Voice",
    "realtime": "OpenAI Realtime tuning",
    "voice_model": "Voice models",
    "agents": "Coding agents",
    "local_agent": "Local agent",
    "model_servers": "Model servers on this machine",
    "owner": "Owner, callers and PIN",
    "phone": "Phone",
    "google": "Google",
    "plugins": "Plugin credentials",
    "projects": "Projects and skills",
    "approvals": "The approval bridge",
    "limits": "Limits and retention",
    "service": "Service, storage and logging",
    "debug": "Development switches",
}


class Tab(NamedTuple):
    """One tab of the Mac app's Configuration screen: its title and the groups it holds."""

    title: str
    groups: tuple[str, ...]


#: The Mac app's tabs, in the order it shows them, each holding one or more `GROUPS` as its
#: sections. Every group is in exactly one tab or owned by exactly one screen
#: (`SCREEN_OF_GROUP`). Within a tab, a field declared `advanced=False` is a key setting,
#: shown first; the rest go under "Advanced".
TABS: dict[str, Tab] = {
    "general": Tab("General", ("owner",)),
    "voice": Tab("Voice", ("voice", "realtime")),
    "agents": Tab("Agents", ("agents", "local_agent")),
    "phone": Tab("Phone", ("phone",)),
    "approvals": Tab("Approvals", ("approvals",)),
    "integrations": Tab("Integrations", ("google", "plugins", "projects")),
    "system": Tab("System", ("limits", "service", "debug")),
}
#: Which tab each group is in: `TABS` read the other way round.
TAB_OF_GROUP = {group: key for key, tab in TABS.items() for group in tab.groups}
#: The Mac app's screens that own groups of settings outright, by title: each is the one
#: editor of its groups' settings, its key rows in place and its advanced ones under an
#: "Advanced" disclosure of its own. Configuration shows none of them, but a search there
#: still finds one and jumps to its screen.
SCREENS: dict[str, str] = {"voice": "Voice", "models": "Local Models"}
#: The groups a screen owns, which sit on no Configuration tab.
SCREEN_OF_GROUP: dict[str, str] = {"voice_model": "voice", "model_servers": "models"}

#: Keys that are never read from, or written to, the configuration files. The PIN has a
#: store of its own (`KERYX_HOME/pin`, keryx.config.pin), written once; a copy in a TOML
#: file would be a second PIN that a text editor can change.
NOT_STORED = frozenset({"KERYX_PIN"})
#: Tables in `config.toml` that are about the settings rather than settings themselves.
META_TABLES = frozenset({"service_writable", "setup"})

#: Where `keryx setup` puts the Google OAuth client file it is handed, in `KERYX_HOME`
#: beside `secrets.toml`: it is configuration, and a secret one. Not `DATA_DIR/google`,
#: which is workspace-mcp's own credentials directory.
GOOGLE_CLIENT_FILE = "google_client_secret.json"
#: The settings that say where Keryx keeps things. Each must be absolute: a relative one
#: would be resolved against whatever directory a process happened to start in, and the
#: service, the CLI and the installers would each find a different one.
DIRECTORY_FIELDS = ("data_dir", "state_dir", "cache_dir")
#: What a working directory held when it was configuration: never read now, and `serve`
#: refuses to start while either is there (`storage_refusal`), until `keryx migrate`.
LEGACY_ENV_FILE = Path(".env")
LEGACY_CLIENT_FILE = Path(".secrets") / "client_secret.json"
LEGACY_WORKING_FILES = (LEGACY_ENV_FILE, LEGACY_CLIENT_FILE)
#: Where a problem with Keryx is filed unless `ISSUE_REPO` says otherwise: its own tracker.
UPSTREAM_REPO = "wak31415/keryx"
#: An assistant's name: a letter, then up to 31 letters, spaces, apostrophes or hyphens.
ASSISTANT_NAME_PATTERN = re.compile(r"[^\W\d_](?:[^\W\d_]|[ '’-]){0,31}")
ASSISTANT_NAME_RULE = (
    "must be 1 to 32 letters, spaces, apostrophes or hyphens, starting with a letter"
)
#: `owner/name`, as GitHub spells a repository.
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
#: The repository root of the running code: `src/keryx/config/settings.py`, three up.
SOURCE_ROOT = Path(__file__).resolve().parents[3]


def setting(
    default: Any = PydanticUndefined,
    description: str = "",
    *,
    title: str,
    group: str,
    advanced: bool,
    service_writable: bool = False,
    **kwargs: Any,
) -> Any:
    """A `Field` carrying the five things `keryx config` needs to know about a setting.

    `advanced` is required, like `title`, so every new field decides it on purpose: key is
    what an owner sets to get a first call working or changes from time to time (keys,
    numbers, names, models, voices, and the privacy and spend that are theirs: retention,
    the daily task cap); advanced is tuning and internals (VAD numbers, server addresses an
    app pane sets in context, ports, timeouts, per-task caps, directories, unit names, the
    log level, debug switches, extra flags).
    """
    assert title.strip(), "a setting needs a title"
    assert group in GROUPS, group
    extra = {
        "title": title,
        "group": group,
        "advanced": advanced,
        "service_writable": service_writable,
    }
    if "default_factory" not in kwargs:
        kwargs["default"] = default
    return Field(description=description, json_schema_extra=extra, **kwargs)


class ConfigFileError(ValueError):
    """A configuration file that does not parse: named, with where, and nothing else."""


#: While set, a configuration file that does not parse is skipped instead of raised: how
#: `keryx doctor` still runs on the machine whose file it has to report.
tolerate_broken_files: ContextVar[bool] = ContextVar("tolerate_broken_files", default=False)


class _TomlLayer(PydanticBaseSettingsSource):
    """One of the two files in `KERYX_HOME`, keyed by environment-variable name.

    Keyed that way because it is the name people know a setting by — the one in the
    README, in `keryx config set`, and in their systemd unit.
    """

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self.path = path

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # pragma: no cover - `__call__` does the reading

    def __call__(self) -> dict[str, Any]:
        # An older name (`FAKE_AGENTS`) is read too, and the current one wins over it.
        fields = {
            key: name
            for name in self.settings_cls.model_fields
            for key in reversed(env_var_names(name))
        }
        values: dict[str, Any] = {}
        try:
            data = read_toml(self.path)
        except tomllib.TOMLDecodeError as error:
            if tolerate_broken_files.get():
                return {}
            raise ConfigFileError(f"{self.path} does not parse ({error})") from None
        except OSError as error:
            # It is there (`read_toml` already turned "not there" into `{}`) but
            # permission, a directory in its place, or anything else the OS refused to
            # open — same treatment as a file that does not parse: every caller that
            # already handles one handles the other. `strerror`, never the error's own
            # `str()`, because that quotes the path back and this message already names it.
            if tolerate_broken_files.get():
                return {}
            raise ConfigFileError(
                f"{self.path} cannot be read ({error.strerror or error})"
            ) from None
        current = sorted(data.items(), key=lambda item: item[0] in fields and (
            item[0] == env_var_name(fields[item[0]])
        ))
        for key, value in current:
            name = fields.get(key)
            # Checked by the current name: `JARVIS_PIN` in a file is no more a PIN than
            # `KERYX_PIN` would be.
            if name is None or env_var_name(name) in NOT_STORED or value == "":
                continue
            values[name] = value
        return values


class Settings(BaseSettings):
    """Keryx runtime configuration. See `docs/configuration.md`."""

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
        env_ignore_empty=True,
        # Almost every field here is a credential, and pydantic quotes the rejected input
        # back in the error it raises. A `KERYX_PIN` that fails the rule below must not
        # end up in a traceback, a journal or a terminal on the way to being fixed.
        hide_input_in_errors=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        home = keryx_home()
        return (
            init_settings,
            env_settings,
            _TomlLayer(settings_cls, secrets_file(home)),
            _TomlLayer(settings_cls, config_file(home)),
            file_secret_settings,
        )

    #: Cached `report_secret_value()`: it is asked for per notification and per report
    #: request, and the fallback lives in a file.
    _report_secret_cache: str | None = PrivateAttr(default=None)

    #: The digits this object took from `KERYX_HOME/pin`, if it took any: what `pin_source`
    #: compares `pin` against. Never read for anything else — `pin` is the one field every
    #: reader asks — and it is a comparison rather than a remembered label so that a copy
    #: carrying a different PIN cannot inherit a source that was true only of the original.
    _adopted_pin: str | None = PrivateAttr(default=None)

    # --- voice -------------------------------------------------------------------------

    openai_api_key: str | None = setting(
        None,
        "OpenAI API key with Realtime access: the voice, unless `VOICE_BASE_URL` names a "
        "server of your own, and the voice model's `web_search`.",
        title="OpenAI API key",
        group="voice", advanced=False,
        repr=False,
    )
    openai_realtime_model: str = setting(
        "gpt-realtime-2.1",
        "The realtime speech-to-speech model a call runs on, on OpenAI. A voice server "
        "elsewhere is sent it too (as `?model=`); the voice pipeline here never is.",
        title="Realtime model",
        group="voice_model", advanced=False
    )
    assistant_name: str = setting(
        DEFAULT_NAME,
        "What the assistant on the phone is called: the name it answers to and introduces "
        "itself by. Lyra and Jarvis each bring a voice of their own; any other name — letters, "
        f"spaces, apostrophes and hyphens, up to 32 — speaks in `OPENAI_VOICE`, or "
        f"`{DEFAULT_VOICE}`.",
        title="Assistant name",
        group="voice", advanced=False,
        service_writable=True,
    )
    openai_voice: str = setting(
        "",
        "The Realtime voice the assistant speaks in. Empty is the assistant's own "
        "(`ASSISTANT_NAME`); a voice the key's organization may not use makes every call fail "
        "to open. Sent to OpenAI only, and for one release to a voice server elsewhere that "
        "has no `VOICE_SERVER_VOICE`.",
        title="Assistant voice",
        group="voice_model", advanced=False,
        service_writable=True,
    )
    openai_transcription_model: str = setting(
        "gpt-4o-mini-transcribe",
        "Transcribes what the caller says, for the call log, on OpenAI; a voice of your own "
        "transcribes with its own speech-to-text.",
        title="Transcription model",
        group="realtime", advanced=True,
    )
    transcription_language: str = setting(
        "",
        "The language you speak on a call, as an ISO-639-1 code (`en`, `de`, `fr`), for the "
        "call log's transcription — which `recall` and the memory read. Empty lets the "
        "transcriber guess each turn. The voice model itself hears the audio either way.",
        title="Transcription language",
        group="voice", advanced=False,
        service_writable=True,
        pattern=r"^([a-z]{2,3})?$",
    )
    clock_format: Literal["24h", "12h"] = setting(
        "24h",
        "How the voice prompt writes the time of day (`14:05` or `2:05 PM`), and so how "
        "the assistant tends to say it.",
        title="Clock format",
        group="voice", advanced=False,
        service_writable=True,
    )
    openai_web_search_model: str = setting(
        "gpt-6-luna",
        "Answers the voice model's own `web_search` tool, through the Responses API (the "
        "Realtime API has no hosted search tool).",
        title="Web search model",
        group="voice", advanced=True,
    )
    #: Was "low" until 2026-08-26, which left about two seconds of silence at the end of
    #: every sentence; "medium" lands near a second and still waits out a pause.
    vad_mode: Literal["server", "semantic"] = setting(
        "semantic",
        "How the assistant decides you have finished: `semantic` waits on whether the sentence "
        "sounds finished, so a pause to think does not cut you off; `server` is a plain "
        "silence timer of `VAD_SILENCE_MS`.",
        title="Turn detection",
        group="realtime", advanced=True,
        service_writable=True,
    )
    vad_eagerness: Literal["low", "medium", "high", "auto"] = setting(
        "medium",
        "Semantic mode only: `low` waits longest, `high` jumps in soonest, `auto` is "
        "`medium`.",
        title="Reply eagerness",
        group="realtime", advanced=True,
        service_writable=True,
    )
    vad_silence_ms: int = setting(
        1200, "Server mode only: the silence, in milliseconds, that ends a turn.",
        title="Silence to end a turn (ms)",
        group="realtime", advanced=True, service_writable=True,
        ge=0,
    )
    vad_threshold: float = setting(
        0.5, "Server mode only: how loud counts as speech (0 to 1).",
        title="Speech threshold",
        group="realtime", advanced=True, service_writable=True,
        ge=0, le=1,
    )
    vad_prefix_ms: int = setting(
        300,
        "Server mode only: audio kept from before speech was detected, so the first "
        "syllable is not clipped.",
        title="Audio before speech (ms)",
        group="realtime", advanced=True,
        service_writable=True,
        ge=0,
    )
    noise_reduction: Literal["auto", "near_field", "far_field", "off"] = setting(
        "auto",
        "Background-noise suppression on what the model hears; `auto` picks `near_field` "
        "for a phone and `far_field` for a microphone across the room. Mostly about "
        "barge-in: noise mistaken for speech is noise that cuts the assistant off.",
        title="Noise reduction",
        group="realtime", advanced=True,
        service_writable=True,
    )

    # --- coding agents -----------------------------------------------------------------

    # A task keeps the agent it started on for its whole life — follow-ups included —
    # because a session id belongs to the agent that issued it.
    agent_backend: AgentName = setting(
        "claude",
        "The coding agent a task runs on when nobody names one out loud. The running "
        "service may switch it only among `AGENTS_ENABLED`.",
        title="Default agent",
        group="agents", advanced=False,
        service_writable=True,
    )
    agents_enabled: Annotated[list[AgentName], NoDecode] = setting(
        description="Every agent a task may be sent to, comma-separated; blank is "
        "`AGENT_BACKEND` alone. `keryx serve` refuses a default that is not in here.",
        title="Enabled agents",
        group="agents", advanced=False,
        default_factory=list,
    )
    #: Claude also has a turn cap and a dollar cap below; Codex has neither, so this is
    #: what bounds it. Generous, because real work — a test suite, a refactor — takes a while.
    subagent_timeout_s: float = setting(
        3 * 60 * 60,
        "The longest one subagent run may take, on any agent, in seconds; 0 is no limit.",
        title="Time limit per task (seconds)",
        group="agents", advanced=True,
        service_writable=True,
        ge=0,
    )
    # Claude. Auth, in order of precedence: ANTHROPIC_API_KEY (pay-per-token) >
    # CLAUDE_CODE_OAUTH_TOKEN (subscription, headless) > the CLI's stored login (default).
    anthropic_api_key: str | None = setting(
        None,
        "Claude, paid per token. Set, it wins over the subscription.",
        title="Anthropic API key",
        group="agents", advanced=False,
        repr=False,
    )
    claude_code_oauth_token: str | None = setting(
        None,
        "Claude on your subscription, for a machine with no browser: the token "
        "`claude setup-token` prints. Blank uses the stored `claude` login.",
        title="Claude subscription token",
        group="agents", advanced=False,
        repr=False,
    )
    claude_model: str = setting(
        "claude-opus-5-5", "The model a Claude task runs on when none is named.",
        title="Claude model",
        group="agents", advanced=False, service_writable=True,
        # `SUBAGENT_MODEL` until 2026-10-07, still read for one release.
        validation_alias=AliasChoices("CLAUDE_MODEL", "SUBAGENT_MODEL"),
    )
    subagent_max_turns: int = setting(
        200, "Agent turns one Claude task may take.", title="Turns per task", group="agents",
        advanced=True,
        ge=1,
    )
    subagent_max_budget_usd: float = setting(
        10.0, "Dollars one Claude task may spend: a runaway cap. On a subscription it is "
        "the SDK's estimate of what the task would have cost, not a charge.",
        title="Budget per task (USD)",
        group="agents", advanced=True,
        gt=0,
    )
    # Codex. The same order: CODEX_API_KEY > CODEX_ACCESS_TOKEN (logged in once into
    # Keryx's own CODEX_HOME) > the CLI's stored login. OPENAI_API_KEY is the voice
    # model's and is never borrowed for this: that would move a subscription onto billing.
    codex_api_key: str | None = setting(
        None,
        "Codex, paid per token. Keryx logs in with it once, into its own `CODEX_HOME`; "
        "`OPENAI_API_KEY` is never used for Codex.",
        title="Codex API key",
        group="agents", advanced=False,
        repr=False,
    )
    codex_access_token: str | None = setting(
        None,
        "Codex on your ChatGPT plan, for a machine with no browser. Blank uses the stored "
        "`codex login`.",
        title="Codex subscription token",
        group="agents", advanced=False,
        repr=False,
    )
    codex_model: str | None = setting(
        None, "The model a Codex task runs on; blank is Codex's own default.",
        title="Codex model",
        group="agents", advanced=False, service_writable=True,
    )

    # --- the voice's models: the Voice screen --------------------------------------------

    # Where a call's audio goes (`VOICE_MODE`, `VOICE_BASE_URL`) and where its words go
    # (`VOICE_LLM`, `VOICE_LLM_BASE_URL`) are protected: pointed elsewhere, either ships
    # every call, the spoken PIN included, to whoever is there.
    #: The setting as stored; `voice_mode` below is the mode a call uses, this or derived.
    voice_mode_explicit: VoiceMode | None = setting(
        None,
        "Where a call's voice runs: `openai` is OpenAI Realtime, `here` the speech pipeline on "
        "this machine (`VOICE_SERVER_*`), `server` a Realtime server elsewhere "
        "(`VOICE_BASE_URL`). Blank is worked out from `VOICE_BASE_URL`, as it was before this "
        "was a setting: blank is OpenAI, this machine's voice port is here, any other address "
        "a server.",
        title="Where the voice runs",
        group="voice_model", advanced=False,
        validation_alias="VOICE_MODE",
    )
    voice_base_url: str | None = setting(
        None,
        "A voice server of your own that speaks the OpenAI Realtime protocol, as its `…/v1` "
        "address (`http://127.0.0.1:8765/v1` is the one `keryx setup` installs). Blank is "
        "OpenAI.",
        title="Voice server URL",
        group="voice_model", advanced=False,
    )
    voice_api_key: str | None = setting(
        None,
        "The Bearer key for that voice server, when it checks one.",
        title="Voice server API key",
        group="voice_model", advanced=False,
        repr=False,
    )
    voice_server_stt: str = setting(
        "parakeet-tdt",
        "How the local voice server hears: speech-to-speech's `--stt` (`parakeet-tdt`, "
        "`whisper`, …).",
        title="Speech-to-text model",
        group="voice_model", advanced=False,
    )
    voice_llm: VoiceLlmSource = setting(
        "shared",
        "Where the words of a voice running here come from: `shared` is the local agent's "
        "model on its server, `here` a model of the voice's own (`VOICE_LLM_MODEL`) on a "
        "server of its own on this machine, `remote` a server elsewhere "
        "(`VOICE_LLM_BASE_URL`) — a box of yours, or a cloud's Responses API.",
        title="Voice LLM",
        group="voice_model", advanced=False,
    )
    voice_llm_model: str | None = setting(
        None,
        "The voice LLM's model. With `VOICE_LLM=here`, a name from `keryx models list` or a "
        "GGUF file's path, served under that name; with `remote`, the server's own name for "
        "it. Not read while the voice LLM is shared.",
        title="Voice LLM model",
        group="voice_model", advanced=False,
    )
    voice_llm_base_url: str | None = setting(
        None,
        "With `VOICE_LLM=remote`, the server the voice's words go to, as its `…/v1` address "
        "(`https://api.openai.com/v1` for OpenAI's).",
        title="Voice LLM server URL",
        group="voice_model", advanced=False,
    )
    voice_llm_api_key: str | None = setting(
        None,
        "The Bearer key for that server, when it checks one. Blank sends `OPENAI_API_KEY` "
        "only when the address is OpenAI's own, and no key of yours anywhere else.",
        title="Voice LLM API key",
        group="voice_model", advanced=False,
        repr=False,
    )
    voice_server_tts: str = setting(
        "kokoro",
        "How the local voice server speaks: speech-to-speech's `--tts` (`kokoro`, `qwen3`, …).",
        title="Text-to-speech model",
        group="voice_model", advanced=False,
    )
    voice_server_voice: str = setting(
        "af_heart",
        "The Kokoro voice the local voice server speaks in (`af_heart`, `bm_george`, …; "
        "`keryx models list` shows them). A voice server elsewhere is sent it too, once it "
        "is set.",
        title="Kokoro voice",
        group="voice_model", advanced=False,
    )
    voice_server_args: str = setting(
        "",
        "More flags for `speech-to-speech serve`, as a command line — a smaller speech model "
        "(`--stt_model_name openai/whisper-base`), a TTS speed. Never a key: it is on argv.",
        title="Extra voice server flags",
        group="voice_model", advanced=True,
    )

    # --- the local agent: Configuration › Agents -----------------------------------------

    local_agent_base_url: str | None = setting(
        None,
        "An inference server for the `local` agent — Ollama, llama.cpp, vLLM, LM Studio — "
        "as `http://gpu-box:11434` or its `…/v1` address. Blank is no local agent.",
        title="Local agent server URL",
        group="local_agent", advanced=True,
    )
    local_agent_api_key: str | None = setting(
        None,
        "The Bearer key for that server, when it checks one. The owner's Anthropic and "
        "OpenAI credentials are never sent there.",
        title="Local agent API key",
        group="local_agent", advanced=True,
        repr=False,
    )
    local_agent_model: str | None = setting(
        None,
        "The model a `local` task runs on, by the server's own name for it.",
        title="Local agent model",
        group="local_agent", advanced=False,
        service_writable=True,
    )
    local_agent_api: LocalAgentApi = setting(
        "anthropic-messages",
        "Which harness drives the local model: `anthropic-messages` runs it inside Claude "
        "Code, `openai-responses` inside Codex. The server must speak that API.",
        title="Local agent harness",
        group="local_agent", advanced=True,
        service_writable=True,
    )

    # --- the model servers on this machine: the Local Models screen ----------------------

    # The servers `keryx setup` runs on this machine (`keryx models serve llm|voice`). Each
    # unit reads these when it starts, so a change needs `systemctl --user restart` of it.
    agent_server_model: str | None = setting(
        None,
        "The model the local agent's llama.cpp server on this machine runs: a name from "
        "`keryx models list`, or a GGUF file's path. Blank runs none.",
        title="Agent model to serve",
        group="model_servers", advanced=False,
        # `LLM_SERVER_MODEL` until 2026-10-07, still read for one release.
        validation_alias=AliasChoices("AGENT_SERVER_MODEL", "LLM_SERVER_MODEL"),
    )
    agent_server_port: int = setting(
        8090, "The port the local agent's llama.cpp server binds, on 127.0.0.1.",
        title="Agent model server port",
        group="model_servers", advanced=True,
        ge=1, le=65535,
        # `LLM_SERVER_PORT` until 2026-10-07, still read for one release.
        validation_alias=AliasChoices("AGENT_SERVER_PORT", "LLM_SERVER_PORT"),
    )
    voice_server_port: int = setting(
        8765, "The port the local voice server binds, on 127.0.0.1.",
        title="Voice server port",
        group="model_servers", advanced=True,
        ge=1, le=65535,
    )
    voice_llm_server_port: int = setting(
        8091, "The port the voice LLM's own llama.cpp server binds, on 127.0.0.1, with "
        "`VOICE_LLM=here`.",
        title="Voice LLM server port",
        group="model_servers", advanced=True,
        ge=1, le=65535,
    )

    # --- owner, callers and PIN ----------------------------------------------------------

    owner_name: str | None = setting(
        None,
        'What the assistant calls you in the prompts it is handed; blank is "the owner". Only a '
        "name: the rest of what it knows about you is its memory.",
        title="Your name",
        group="owner", advanced=False,
    )
    allowed_callers: Annotated[list[str], NoDecode] = setting(
        description="Your phone numbers in E.164 (`+15551234567`), comma-separated. Every "
        "other caller is refused. This is the handsets one person picks up, not a guest "
        "list: a call Keryx places to any of them reached you.",
        title="Your phone numbers",
        group="owner", advanced=False,
        default_factory=list,
    )
    owner_number_explicit: str | None = setting(
        None,
        "Which of your numbers Keryx rings first; blank is the first of "
        "`ALLOWED_CALLERS`.",
        title="Number to ring first",
        group="owner", advanced=False,
        validation_alias="OWNER_NUMBER",
    )
    #: `KERYX_PIN` always wins; with it unset this is filled from `KERYX_HOME/pin` by
    #: `_resolve_pin` below, so every reader of `settings.pin` sees one source.
    pin: str | None = setting(
        None,
        "The phone PIN: 6 to 8 digits, asked for before anything is dispatched from a "
        "call. Not kept in the configuration files: it lives in `KERYX_HOME/pin`, written "
        "once by `keryx setup` or by the first call, and this variable, set in the "
        "environment, overrides that file. With no PIN anywhere nothing of yours is read "
        "out on the phone and every dispatch is refused.",
        title="Phone PIN",
        group="owner", advanced=False,
        # `JARVIS_PIN` was its name before the service was Keryx, and a unit or a shell
        # profile from then still sets it: read, never written, and the new name wins.
        validation_alias=AliasChoices("KERYX_PIN", "JARVIS_PIN"),
        repr=False,
    )
    pin_failure_limit: int = setting(
        10,
        "Wrong PINs, counted across every call, before PIN entry locks for everyone — "
        "the right PIN included.",
        title="Wrong PINs before lockout",
        group="owner", advanced=True,
        ge=1,
    )
    pin_failure_window_hours: float = setting(
        24, "How long a wrong PIN is remembered.",
        title="Wrong PINs remembered (hours)",
        group="owner", advanced=True, gt=0
    )
    pin_lockout_minutes: float = setting(
        60, "How long PIN entry stays locked once the limit is reached.",
        title="Lockout length (minutes)",
        group="owner", advanced=True, gt=0
    )
    #: On by design (the owner's ruling, 2026-09-19): the PIN's job is to stop a phone-side
    #: caller-id spoofer *acting*; against somebody who has the machine it buys nothing.
    briefing_before_pin: bool = setting(
        True,
        "Whether a call is handed its standing briefing — unreported results, the memory, "
        "project names, briefs, skills — before the PIN. The PIN is the line between "
        "reading and acting; off means nothing of yours is said until it is given. Does "
        "nothing until a PIN exists.",
        title="Briefing before the PIN",
        group="owner", advanced=False,
    )

    # --- phone ---------------------------------------------------------------------------

    twilio_account_sid: str | None = setting(
        None, "Your Twilio account SID (`AC…`).", title="Twilio account SID", group="phone",
        advanced=False,
    )
    twilio_auth_token: str | None = setting(
        None, "Your Twilio auth token.", title="Twilio auth token", group="phone",
        advanced=False, repr=False,
    )
    twilio_number: str | None = setting(
        None, "The Twilio number Keryx answers and calls from, in E.164.",
        title="Twilio number",
        group="phone", advanced=False
    )
    sms_enabled: bool = setting(
        False,
        "Whether Keryx may text at all. Off: many Twilio accounts cannot send SMS in "
        "their region, and Slack is the written channel. Outbound calls are unaffected.",
        title="Text messages (SMS)",
        group="phone", advanced=False,
    )
    public_host: str | None = setting(
        None,
        "The public hostname Twilio reaches this machine on, e.g. `keryx.example.com`: "
        "the name routed to your tunnel.",
        title="Public hostname",
        group="phone", advanced=False,
    )
    cloudflare_tunnel: str = setting(
        "keryx",
        "The Cloudflare tunnel the service scripts run (`scripts/dev.sh`, "
        "`scripts/install-systemd.sh`).",
        title="Cloudflare tunnel",
        group="phone", advanced=True,
    )
    host: str = setting(
        "127.0.0.1", "The address the phone server binds.",
        title="Phone server address",
        group="phone", advanced=True,
    )
    port: int = setting(
        8080, "The port the phone server binds.",
        title="Phone server port",
        group="phone", advanced=True, ge=1, le=65535,
    )

    # --- google -------------------------------------------------------------------------

    google_oauth_client_id: str | None = setting(
        None,
        "The Google OAuth client id. Blank: read from `GOOGLE_CLIENT_SECRETS_FILE`.",
        title="OAuth client ID",
        group="google", advanced=False,
    )
    google_oauth_client_secret: str | None = setting(
        None, "The Google OAuth client secret, with the id above.",
        title="OAuth client secret",
        group="google", advanced=False, repr=False
    )
    google_client_secrets_file: Path | None = setting(
        None,
        "The OAuth client JSON the Google Cloud console downloads (desktop or web shape). "
        f"Blank: `KERYX_HOME/{GOOGLE_CLIENT_FILE}`, where `keryx setup` puts it.",
        title="OAuth client file",
        group="google", advanced=True,
    )
    user_google_email: str | None = setting(
        None,
        "The Google account agents act as; `keryx setup` fills it in from the sign-in.",
        title="Google account",
        group="google", advanced=False,
    )
    #: Off since 2026-08-24: the Claude CLI already carries authorized claude.ai
    #: Gmail/Calendar/Drive connectors, so this only added a second path to trip over.
    google_workspace_mcp: bool = setting(
        False,
        "Give every subagent the workspace-mcp server (send mail, manage the calendar). "
        "Needed for Codex, which has no claude.ai connectors; Claude already has them.",
        title="Workspace MCP server",
        group="google", advanced=False,
    )

    # --- plugin credentials --------------------------------------------------------------

    # Only the secrets: every other plugin setting is in the plugin's own TOML beside it in
    # `DATA_DIR/tools` (`keryx.plugins`). These stay here so they live in `secrets.toml`.
    slack_bot_token: str | None = setting(
        None,
        "The bot token the `send_to_slack` plugin posts with (and the PIN-lockout alert).",
        title="Slack bot token",
        group="plugins", advanced=False,
        repr=False,
    )
    # Read-only, and on an *admin*-scoped credential: the key the voice agent talks to the
    # model with cannot read `/v1/organization/costs`.
    openai_admin_key: str | None = setting(
        None,
        "An OpenAI *admin* key for the `check_billing` plugin; the ordinary key gets a 401 "
        "on the costs endpoint.",
        title="OpenAI admin key",
        group="plugins", advanced=False,
        repr=False,
    )
    anthropic_admin_key: str | None = setting(
        None, "An `sk-ant-admin…` key, for what the subagents have cost (`check_billing`).",
        title="Anthropic admin key",
        group="plugins", advanced=False, repr=False,
    )

    # --- projects ------------------------------------------------------------------------

    projects: dict[str, str] = setting(
        description='Spoken project names for repositories outside `PROJECTS_ROOT`, as '
        '`{"name": "/path"}`.',
        title="Other projects",
        group="projects", advanced=False,
        default_factory=dict,
    )
    projects_root: Path = setting(
        Path("~/projects"),
        "Where a task with no project starts; each subdirectory is a project you can name. "
        "Never created: without it, such a task starts in `DATA_DIR/workspace`.",
        title="Projects folder",
        group="projects", advanced=False,
    )
    skills_dir: Path = setting(
        description="Where the Claude CLI keeps its skills; listed in the voice prompt so "
        "the assistant knows what the subagents are good at.",
        title="Skills folder",
        group="projects", advanced=True,
        default_factory=lambda: claude_config_dir() / "skills",
    )
    keryx_checkout: Path | None = setting(
        None,
        "The Keryx repository on this machine, which a subagent reads when it reports a "
        "problem with Keryx. Unset: the checkout Keryx runs from, when it runs from one.",
        title="Keryx checkout",
        group="projects", advanced=True,
        validation_alias=AliasChoices("KERYX_CHECKOUT", "JARVIS_CHECKOUT"),
    )
    issue_reporting: bool = setting(
        False,
        "Whether a bug or a feature request for Keryx, said on a call, may be filed as a "
        "GitHub issue by a subagent, with `gh`. `keryx setup` asks; only you can turn it on.",
        title="GitHub issue reports",
        group="projects", advanced=False,
    )
    issue_repo: str = setting(
        UPSTREAM_REPO,
        "The GitHub repository (`owner/name`) those issues are filed on.",
        title="Issue repository",
        group="projects", advanced=True,
    )

    # --- the approval bridge -------------------------------------------------------------

    approvals_enabled: bool = setting(
        True,
        "Whether a Claude Code prompt left unanswered on your screen may become a call. "
        "The hook is installed separately (`scripts/install-claude-hook.sh`).",
        title="Approval calls",
        group="approvals", advanced=False,
    )
    approval_escalate_seconds: float = setting(
        300, "How long a prompt waits on screen before Keryx rings about it.",
        title="Ring after (seconds)",
        group="approvals", advanced=True,
    )
    approval_call_window_seconds: float = setting(
        240,
        "How long the hook keeps waiting after that. The two together are the longest the "
        "hook blocks; its `timeout` in `~/.claude/settings.json` must exceed their sum.",
        title="Answer window (seconds)",
        group="approvals", advanced=True,
    )
    approval_max_per_hour: int = setting(
        4, "Approval calls per hour, however many prompts pile up.",
        title="Maximum calls per hour",
        group="approvals", advanced=True,
        ge=0,
    )
    approval_quiet_hours: str | None = setting(
        None,
        "`HH:MM-HH:MM` in which it never rings (may cross midnight), in the host's time.",
        title="Quiet hours",
        group="approvals", advanced=False,
        service_writable=True,
    )
    approval_bash_allow: Annotated[list[str], NoDecode] = setting(
        description="The only shell commands a keypad digit may ever run, matched word for "
        "word; only `git push` and `git commit` may carry arguments "
        "(`keryx/approvals/policy.py`). This allowlist is the primary control: widening "
        "it widens exactly what a phone keypad can execute.",
        title="Allowed commands",
        group="approvals", advanced=True,
        default_factory=lambda: ["git push", "git commit"],
    )
    approval_roots: Annotated[list[str], NoDecode] = setting(
        description="Where a file may be written by phone approval; blank is "
        "`PROJECTS_ROOT` plus every project in `PROJECTS`.",
        title="Writable folders",
        group="approvals", advanced=True,
        default_factory=list,
    )

    # --- limits and retention ------------------------------------------------------------

    max_concurrent_tasks: int = setting(
        3, "Subagents running at once; the rest queue.",
        title="Tasks at once",
        group="limits", advanced=True, service_writable=True,
        ge=1,
    )
    dispatch_wait_max_seconds: int = setting(
        25, "How long a call waits for a short task to answer inline.",
        title="Inline answer wait (seconds)",
        group="limits", advanced=True,
        ge=0,
    )
    local_silence_timeout: float = setting(
        30,
        "Seconds of silence that end a local session; 0 never does.",
        title="Local session timeout (seconds)",
        group="limits", advanced=True,
        service_writable=True,
        ge=0,
    )
    max_call_seconds: float = setting(
        1800,
        "The longest a phone call may run; Keryx wraps up 30 s before. 0 is no limit.",
        title="Maximum call length (seconds)",
        group="limits", advanced=True,
        service_writable=True,
        ge=0,
    )
    max_phone_sessions: int = setting(
        2,
        "Phone calls open at once, each one a realtime session being paid for; another "
        "hears that the line is busy.",
        title="Phone calls at once",
        group="limits", advanced=True,
        ge=1,
    )
    daily_task_cap: int = setting(
        50, "Tasks dispatched per day.", title="Tasks per day", group="limits", advanced=False, ge=0
    )
    # Both off at 0, which is what Keryx has always done: a default that deleted
    # someone's own call transcripts because nobody changed a number is not one worth
    # having. See also `keryx forget`.
    transcript_retention_days: int = setting(
        0,
        "Delete call transcripts older than this many days; 0 keeps everything.",
        title="Keep transcripts for (days)",
        group="limits", advanced=False,
    )
    task_retention_days: int = setting(
        0,
        "Delete finished tasks older than this; 0 keeps everything. A result you have not "
        "been told about is never deleted.",
        title="Keep finished tasks for (days)",
        group="limits", advanced=False,
    )

    # --- service, storage and logging ----------------------------------------------------

    data_dir: Path = setting(
        description="Where tasks, transcripts, memory and sign-in tokens are kept, owner-only "
        "(0700, files 0600).",
        title="Data folder",
        group="service", advanced=True,
        default_factory=default_data_dir,
    )
    state_dir: Path = setting(
        description="Where the logs, the restart record and the approval bridge's socket "
        "are kept, owner-only.",
        title="State folder",
        group="service", advanced=True,
        default_factory=default_state_dir,
    )
    cache_dir: Path = setting(
        description="Where what can be downloaded again is kept.",
        title="Cache folder",
        group="service", advanced=True,
        default_factory=default_cache_dir,
    )
    service_manager: Literal["auto", "systemd", "launchd", "none"] = setting(
        "auto",
        "What `keryx restart` asks to restart this process: `auto` is systemd or launchd, "
        "but only for a process actually running under it; `none` refuses.",
        title="Service manager",
        group="service", advanced=True,
    )
    service_unit: str | None = setting(
        None,
        "The systemd unit or launchd label; blank is the installed default "
        "(`keryx.service`, `dev.keryx.agent`).",
        title="Service unit or label",
        group="service", advanced=True,
    )
    report_secret: str | None = setting(
        None,
        "Signs the `/reports/{id}` links texted to you; blank generates one into "
        "`DATA_DIR/report_secret`.",
        title="Report link secret",
        group="service", advanced=True,
        repr=False,
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = setting(
        "INFO", "How much `STATE_DIR/logs/keryx.log` says.", title="Log level", group="service",
        advanced=True,
        service_writable=True,
    )

    # --- development switches ------------------------------------------------------------

    debug_skip_twilio_validation: bool = setting(
        False,
        "Skip Twilio's request signatures, for a machine nothing outside can reach. "
        "`keryx serve` refuses the phone with it on behind a `PUBLIC_HOST`.",
        title="Skip Twilio signatures",
        group="debug", advanced=True,
    )
    demo_mode: bool = setting(
        False,
        "Try the phone before a coding agent is set up: calls, the PIN and call-backs are "
        "real, and every task comes back after a moment with a sample answer that says it "
        "was a demo. No agent runs and no agent tokens are spent. `keryx serve --demo` turns "
        "it on for one run.",
        title="Demo mode",
        group="debug", advanced=True,
        # `FAKE_AGENTS` until 2026-09-29, still read for one release.
        validation_alias=AliasChoices("DEMO_MODE", "FAKE_AGENTS"),
    )

    # --- validation ----------------------------------------------------------------------

    @field_validator("allowed_callers", "approval_bash_allow", "approval_roots", mode="before")
    @classmethod
    def _parse_comma_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("agents_enabled", mode="before")
    @classmethod
    def _parse_agent_list(cls, value: object) -> object:
        """A comma list, case-insensitive; a name Keryx does not know fails the load."""
        if isinstance(value, str):
            value = value.split(",")
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        return value

    @field_validator("assistant_name", mode="after")
    @classmethod
    def _assistant_name_is_a_name(cls, value: str) -> str:
        """A name the prompt can say: no digits, no braces, and not a coding agent's.

        An assistant called Claude would hand its work to Claude and hear itself named in
        every result, so the agents' names are refused. A built-in persona's name is spelled
        its way (`jarvis` is Jarvis), and runs of spaces are collapsed.
        """
        name = " ".join(value.split())
        if not ASSISTANT_NAME_PATTERN.fullmatch(name):
            raise ValueError(ASSISTANT_NAME_RULE)
        if name.lower() in get_args(AgentName):
            raise ValueError(f"{name} is a coding agent's name; the assistant needs its own")
        persona = PERSONAS.get(name.lower())
        return persona.name if persona is not None else name

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level_is_uppercase(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("agent_backend", mode="before")
    @classmethod
    def _agent_name_is_lowercase(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("projects", mode="before")
    @classmethod
    def _parse_json_map(cls, value: object) -> object:
        """A JSON object given as text — `keryx config set PROJECTS '{"a": "/b"}'`."""
        if isinstance(value, str):
            try:
                return json.loads(value) if value.strip() else {}
            except json.JSONDecodeError as error:
                raise ValueError('must be a JSON object, like {"name": "value"}') from error
        return value

    @field_validator(*OPTIONAL_STR_FIELDS, "voice_mode_explicit", mode="before")
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
        """A configured PIN must conform, or Keryx does not start.

        Blank is handled upstream by `_blank_is_unset` and stays "no PIN", which is a
        different and safe thing: it refuses every dispatch from the phone. What this
        refuses is a PIN that is *set* and not worth having.
        """
        if value is None or PIN_PATTERN.fullmatch(value):
            return value
        raise ValueError(PIN_RULE)

    @field_validator("voice_base_url", "voice_llm_base_url", "local_agent_base_url",
                     mode="after")
    @classmethod
    def _base_url_is_an_address(cls, value: str | None) -> str | None:
        """An `http://` or `https://` address, kept as its `…/v1` root."""
        if value is None:
            return None
        return Endpoint.parse(value).base_url

    @field_validator("google_client_secrets_file", "keryx_checkout", mode="before")
    @classmethod
    def _blank_path_is_unset(cls, value: object) -> object:
        """A blank path is no path, not the current directory."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        *DIRECTORY_FIELDS,
        "projects_root",
        "skills_dir",
        "google_client_secrets_file",
        "keryx_checkout",
        mode="after",
    )
    @classmethod
    def _expand_path(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @field_validator(*DIRECTORY_FIELDS, "keryx_checkout", mode="after")
    @classmethod
    def _directory_is_absolute(cls, value: Path | None) -> Path | None:
        """One place resolves a directory setting, and it resolves no relative one."""
        if value is not None and not value.is_absolute():
            raise ValueError("must be an absolute path (or start with ~)")
        return value

    @field_validator("issue_repo", mode="after")
    @classmethod
    def _repo_is_owner_slash_name(cls, value: str) -> str:
        """`owner/name`: it is handed to `gh --repo` as it is."""
        value = value.strip()
        if not REPO_PATTERN.fullmatch(value):
            raise ValueError("must be a GitHub repository as owner/name")
        return value

    @model_validator(mode="after")
    def _resolve_pin(self) -> "Settings":
        """Fill `pin` from `KERYX_HOME/pin` when the environment set none.

        Two sources, one field, resolved once here so that every existing reader of
        `settings.pin` — the session's compare, the gates, the transcript redaction — keeps
        working without knowing there are two. `KERYX_PIN` wins: it is the owner at the
        keyboard, and it outranks anything a call enrolled.
        """
        if not self.pin and (enrolled := read_enrolled_pin(self.config_dir)) is not None:
            self.pin, self._adopted_pin = enrolled, enrolled
        return self

    # --- the PIN -------------------------------------------------------------------------

    @property
    def config_dir(self) -> Path:
        """`KERYX_HOME`: the store, the PIN and the Google client file.

        Not a field: it is what says where the fields are read from, so only the
        environment can move it (`keryx.config.files.keryx_home`).
        """
        return keryx_home()

    @property
    def pin_source(self) -> str | None:
        """Where the PIN in hand came from (`PIN_FROM_*`), or None when there is none.

        A comparison rather than a label written down when the PIN was resolved, because
        the PIN can be replaced on a copy of these settings — `model_copy(update={"pin":
        …})`, which is how `doctor` and its tests build each state — and a remembered
        source would describe a PIN the copy does not have.
        """
        if not self.pin:
            return None
        return PIN_FROM_FILE if self.pin == self._adopted_pin else PIN_FROM_ENV

    @property
    def pin_enrolment_open(self) -> bool:
        """Whether a call may still set the first PIN: none configured, and none on disk.

        Both halves, because they are different facts. An unusable file is no PIN *and* no
        enrolment: it seals the door exactly as a good one does (`write_enrolled_pin`).

        A PIN still in the legacy `~/.jarvis` seals it too. That machine has a PIN; it has
        simply not been moved yet (`keryx migrate`), and a caller must not be the one to
        choose a new one in the meantime. `serve` refuses to start in that state anyway
        (`storage_refusal`); this is the door's own belt to that.
        """
        return (
            not self.pin
            and not pin_file(self.config_dir).exists()
            and not pin_file(legacy_home()).exists()
        )

    @property
    def reads_before_pin(self) -> bool:
        """Whether a call below `FULL` may be handed what Keryx knows about its owner.

        `BRIEFING_BEFORE_PIN` is the setting, and its reasoning presumes a PIN exists:
        the PIN is the line between reading and acting, so reads may come first. With no
        PIN anywhere there is no line and no authentication at all — every allowed caller
        would be handed the memory, the digest and the project names for ever, with no way
        to prove anything. So until one exists, the standing briefing is withheld, and the
        read-only tools over the same material are refused with it.
        """
        return self.briefing_before_pin and bool(self.pin)

    def enrol_pin(self, digits: str) -> bool:
        """Set the first PIN this machine has had. False when one exists.

        The file is the record; this also adopts the PIN in the process that wrote it, so
        `keryx serve` — which holds one `Settings` from startup — compares against it,
        redacts it out of transcripts and closes the door without waiting for a restart.
        """
        if not write_enrolled_pin(self.config_dir, digits):
            return False
        self.pin = self._adopted_pin = digits
        return True

    # --- derived -------------------------------------------------------------------------

    def google_client_file(self) -> Path | None:
        """The OAuth client JSON in use: the setting, else setup's copy beside the store."""
        if self.google_client_secrets_file is not None:
            return self.google_client_secrets_file
        path = self.config_dir / GOOGLE_CLIENT_FILE
        return path if path.is_file() else None

    def google_oauth_client(self) -> tuple[str, str] | None:
        """The OAuth client as `(id, secret)`: the configured pair if set, else the JSON file.

        The console hands out that file with the pair nested under `installed` (desktop
        clients) or `web`; either shape is accepted. A missing or malformed file simply
        means "no Google", which the doctor reports and Gmail/Calendar work refuses.
        """
        if self.google_oauth_client_id and self.google_oauth_client_secret:
            return self.google_oauth_client_id, self.google_oauth_client_secret
        path = self.google_client_file()
        if path is None:
            return None
        try:
            return parse_google_client(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not use the Google client secrets at %s: %s", path, exc)
            return None

    @property
    def voice_mode(self) -> VoiceMode:
        """Where a call's voice runs: `VOICE_MODE`, else worked out from `VOICE_BASE_URL`
        as it was before that was a setting — blank is OpenAI, the address of this
        machine's voice port is here, and any other address is a server elsewhere."""
        if self.voice_mode_explicit is not None:
            return self.voice_mode_explicit
        if self.voice_base_url == self.voice_here_url:
            return "here"
        return "server" if self.voice_base_url else "openai"

    @property
    def voice_here_url(self) -> str:
        """The address of the voice pipeline Keryx runs here, derived from its port."""
        return f"http://{LOOPBACK_HOST}:{self.voice_server_port}/v1"

    @property
    def voice(self) -> str:
        """The voice a call's `session.update` asks for; blank is the server's own.

        OpenAI's (`OPENAI_VOICE`, else the assistant's persona) only where the voice is
        OpenAI's. The pipeline here speaks in `VOICE_SERVER_VOICE`, on its own command line,
        and honours a per-session voice — so anything sent here would override it: blank. A
        server elsewhere is sent `VOICE_SERVER_VOICE` when that was set, else `OPENAI_VOICE`
        for one release (what it was sent before), else nothing: the persona's names
        (`marin`, `cedar`) are OpenAI's, and a server that does not know one speaks nothing.
        """
        endpoint = self.voice_endpoint
        if endpoint is not None and endpoint.is_openai:
            return self.openai_voice or voice_for(self.assistant_name)
        if self.voice_mode == "server":
            if "voice_server_voice" in self.model_fields_set:
                return self.voice_server_voice
            return self.openai_voice
        return ""

    @property
    def voice_transcription_model(self) -> str | None:
        """`OPENAI_TRANSCRIPTION_MODEL`, where the voice is OpenAI's; None anywhere else,
        which leaves the transcription block out of `session.update` (a self-hosted voice
        transcribes with its own speech-to-text)."""
        endpoint = self.voice_endpoint
        if endpoint is not None and endpoint.is_openai:
            return self.openai_transcription_model
        return None

    @property
    def openai_key(self) -> str | None:
        """`OPENAI_API_KEY`, or None when it is unset (or the read-only loader's stand-in)."""
        key = self.openai_api_key
        return key if key and key != PLACEHOLDER_KEY else None

    @property
    def voice_endpoint(self) -> Endpoint | None:
        """Where a call's audio goes, by `voice_mode`; None for a `server` with no
        `VOICE_BASE_URL`, which `voice_refusal` refuses.

        OpenAI with `OPENAI_API_KEY`; the pipeline here on its derived loopback address with
        no model (`?model=` is left off: it picks none); a server elsewhere at
        `VOICE_BASE_URL`, still sent `OPENAI_REALTIME_MODEL`, since such a server may choose
        its model by it. Both of the owner's own are sent `VOICE_API_KEY`, as they were: a
        pipeline here given one through `VOICE_SERVER_ARGS` checks it.
        """
        mode = self.voice_mode
        if mode == "here":
            return Endpoint(self.voice_here_url, self.voice_api_key)
        if mode == "server":
            if not self.voice_base_url:
                return None
            return Endpoint.parse(
                self.voice_base_url, api_key=self.voice_api_key, model=self.openai_realtime_model
            )
        return Endpoint(OPENAI_BASE_URL, self.openai_key, self.openai_realtime_model)

    @property
    def local_agent_endpoint(self) -> Endpoint | None:
        """The `local` agent's server, or None when `LOCAL_AGENT_BASE_URL` is blank."""
        if not self.local_agent_base_url:
            return None
        return Endpoint.parse(
            self.local_agent_base_url,
            api_key=self.local_agent_api_key,
            model=self.local_agent_model or "",
        )

    @property
    def voice_llm_endpoint(self) -> Endpoint | None:
        """Where the voice pipeline here asks for its words, by `VOICE_LLM`, over the
        Responses API (`Endpoint.api`); None when the source has no address to give.

        `shared` is the local agent's server, as it always was; `here` a server of the
        voice's own on `VOICE_LLM_SERVER_PORT`, serving `VOICE_LLM_MODEL`, with no key;
        `remote` `VOICE_LLM_BASE_URL` with `VOICE_LLM_MODEL` and `voice_llm_key`.
        """
        if self.voice_llm == "shared":
            words = self.local_agent_endpoint
        elif self.voice_llm == "here":
            words = Endpoint(f"http://{LOOPBACK_HOST}:{self.voice_llm_server_port}/v1",
                             model=self.voice_llm_model or "")
        elif self.voice_llm_base_url:
            words = Endpoint.parse(self.voice_llm_base_url, model=self.voice_llm_model or "")
            words = replace(words, api_key=self.voice_llm_key(words))
        else:
            words = None
        return None if words is None else replace(words, api=RESPONSES_API)

    def voice_llm_key(self, endpoint: Endpoint) -> str | None:
        """The key a remote voice LLM is sent: `VOICE_LLM_API_KEY`, else `OPENAI_API_KEY`
        only when `endpoint` is OpenAI's own host, else none (the placeholder). The owner's
        OpenAI key never goes to a server that is not OpenAI's."""
        if self.voice_llm_api_key:
            return self.voice_llm_api_key
        return self.openai_key if endpoint.is_openai else None

    def voice_refusal(self) -> str | None:
        """Why no call could open, in one line; None when the voice has somewhere to go.

        Each mode is refused only for its own settings, and the pipeline here for its
        words' (`VOICE_LLM`) as well, since a pipeline with none answers a call with silence.
        """
        mode = self.voice_mode
        if mode == "openai":
            if self.openai_key:
                return None
            if self.voice_mode_explicit is None:
                return (
                    "OPENAI_API_KEY is not set and VOICE_BASE_URL names no voice server of "
                    "your own, so no call could open — `keryx setup`"
                )
            return ("VOICE_MODE is openai and OPENAI_API_KEY is not set, so no call could "
                    "open — `keryx setup`")
        if mode == "server":
            if self.voice_base_url:
                return None
            return ("VOICE_MODE is server and VOICE_BASE_URL names no server, so no call "
                    "could open — `keryx setup`")
        return self._voice_llm_refusal()

    def _voice_llm_refusal(self) -> str | None:
        """Why the pipeline here would have no words, in one line; None when it has some."""
        if self.voice_llm == "here" and not self.voice_llm_model:
            missing = "VOICE_LLM_MODEL"
        elif self.voice_llm == "remote" and not self.voice_llm_base_url:
            missing = "VOICE_LLM_BASE_URL"
        elif self.voice_llm == "remote" and not self.voice_llm_model:
            missing = "VOICE_LLM_MODEL"
        else:
            return None
        return (f"VOICE_LLM is {self.voice_llm} and {missing} is not set, so the voice here "
                "would have no words to answer a call with — `keryx setup`")

    @property
    def owner_label(self) -> str:
        """`OWNER_NAME` as the prompts say it, else `OWNER_FALLBACK`."""
        return self.owner_name.strip() if self.owner_name else OWNER_FALLBACK

    @property
    def owner_numbers(self) -> tuple[str, ...]:
        """Every phone that is the owner's own: the allowlist, plus an explicit `OWNER_NUMBER`.

        Keryx has one owner. `ALLOWED_CALLERS` is not a guest list — it is the set of
        handsets one person picks up, and more than one entry means they carry more than
        one phone. So a call Keryx placed to any of them reached *them*, and `OWNER_NUMBER`
        decides only which one it rings first. Possession is judged against this, never
        against that one (`keryx.stream_tokens.confers_possession`).
        """
        numbers = list(self.allowed_callers)
        explicit = self.owner_number_explicit
        if explicit and explicit not in numbers:
            numbers.insert(0, explicit)
        return tuple(numbers)

    @property
    def owner_number(self) -> str | None:
        """The one to ring: explicit `OWNER_NUMBER`, else the first allowed caller, else None."""
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
                "can reach the tunnel could pose as Twilio — turn it off"
            )
        return None

    def storage_refusal(self, working_dir: Path | None = None) -> str | None:
        """Why `keryx serve` must not start until `keryx migrate` has run; None if it may.

        Two states, both of an install from before the XDG layout. `~/.jarvis` still holding
        what Keryx put there, while neither `KERYX_HOME` nor `DATA_DIR` names it: started
        now, the service would find an empty data directory — no tasks, no memory, and no
        PIN, which is an open enrolment door. Or a `.env` or `.secrets/client_secret.json`
        in the working directory: configuration this build never reads, so it would start
        without it.

        The signal is the old files being there, never the new directory being missing:
        `ensure_dirs` creates that on the first command of any kind. A `JARVIS_HOME` with no
        `KERYX_HOME` beside it is refused as well: it names a configuration directory under
        the old name, which is never followed.
        """
        if stray := stray_legacy_home_env():
            return (
                f"{LEGACY_HOME_ENV}={stray} is set and {HOME_ENV} is not — Keryx reads only "
                f"{HOME_ENV}, so rename it"
            )
        in_use = (self.config_dir, self.data_dir, self.state_dir, self.cache_dir)
        for old in renamed_dirs().values():
            if old not in in_use and holds_files(old):
                return (
                    f"{old} still holds Keryx's files from when it was Jarvis — run "
                    "`keryx migrate`"
                )
        legacy = legacy_home()
        found = legacy_entries(legacy)
        if found and legacy not in (self.config_dir, self.data_dir):
            shown = ", ".join(found[:3]) + (", …" if len(found) > 3 else "")
            return f"{legacy} still holds Keryx's files ({shown}) — run `keryx migrate`"
        working = Path.cwd() if working_dir is None else working_dir
        for name in LEGACY_WORKING_FILES:
            if (working / name).is_file():
                return (
                    f"{working / name} is configuration Keryx no longer reads — run "
                    "`keryx migrate`"
                )
        return None

    @property
    def enabled_agents(self) -> tuple[str, ...]:
        """Every agent a task may run on, the default first; `AGENTS_ENABLED` blank is it alone."""
        names = [self.agent_backend, *self.agents_enabled]
        return tuple(dict.fromkeys(names)) if self.agents_enabled else (self.agent_backend,)

    def agent_refusal(self) -> str | None:
        """Why `keryx serve` must not start with these agents, in one line; None if it may.

        A default that is not enabled is a contradiction: every task nobody named an agent
        for would be sent to one this process was told not to run. A default whose SDK (its
        extra) is not installed could run nothing at all. `--demo` runs no real agent,
        so it needs neither.
        """
        if self.agents_enabled and self.agent_backend not in self.agents_enabled:
            return (
                f"AGENT_BACKEND is {self.agent_backend}, which AGENTS_ENABLED "
                f"({', '.join(self.agents_enabled)}) leaves out — add it, or pick one of those"
            )
        # Imported here: the registry imports the agents, which import this module.
        from keryx.agents.registry import install_command, installed

        if self.demo_mode:
            return None
        if not installed(self.agent_backend, self):
            return (
                f"AGENT_BACKEND is {self.agent_backend}, which is not installed — "
                f"{install_command(self.agent_backend, self)}"
            )
        if self.agent_backend == "local" and not self.local_agent_base_url:
            return (
                "AGENT_BACKEND is local, but LOCAL_AGENT_BASE_URL names no server — set it, "
                "or `keryx setup`"
            )
        return None

    def noise_reduction_for(self, channel: str) -> Literal["near_field", "far_field"] | None:
        """The noise-reduction profile for `channel`, or None to leave it off."""
        if self.noise_reduction == "off":
            return None
        if self.noise_reduction != "auto":
            return self.noise_reduction
        return "near_field" if channel == "phone" else "far_field"

    @property
    def custom_tools_dir(self) -> Path:
        """`DATA_DIR/tools`: the owner's own voice tools (`keryx.tools.custom`)."""
        return self.data_dir / "tools"

    @property
    def checkout(self) -> Path | None:
        """The Keryx repository: `KERYX_CHECKOUT`, else the one this code runs from.

        None when neither is one — an install from a wheel, with no setting pointing at a
        clone — because then there is no source tree to read and no skills beside it.
        """
        if self.keryx_checkout is not None:
            return self.keryx_checkout
        return SOURCE_ROOT if (SOURCE_ROOT / "pyproject.toml").is_file() else None

    def ensure_dirs(self) -> None:
        """Create `data_dir` (`tasks`, `calls`, `tools`) and `state_dir` (`logs`, `approvals`).

        All owner-only. Existing directories are tightened in place, so an install made
        before this simply becomes private the next time anything starts. The cache
        directory is made by what downloads into it, when it does.
        """
        for root, names in ((self.data_dir, ("tasks", "calls", "tools")),
                            (self.state_dir, ("logs", "approvals"))):
            secure_dir(root)
            for name in names:
                secure_dir(root / name)

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

        secret = secrets.token_hex(32)
        write_private(secret_path, secret)
        return secret


def parse_google_client(text: str) -> tuple[str, str]:
    """`(client_id, client_secret)` from a Google OAuth client JSON; ValueError if it is not one.

    The console nests the pair under `installed` (a desktop client) or `web`.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("that is not JSON") from error
    block = (data.get("installed") or data.get("web") or data) if isinstance(data, dict) else {}
    pair = (block.get("client_id"), block.get("client_secret")) if isinstance(block, dict) else ()
    if not (len(pair) == 2 and all(isinstance(part, str) and part for part in pair)):
        raise ValueError("it has no client_id and client_secret")
    client_id, client_secret = pair
    return client_id, client_secret


def env_var_name(field_name: str) -> str:
    """The name a `Settings` field is set under: in the environment, the store and the CLI.

    Its validation alias where it has one (`pin` is `KERYX_PIN`), else the field name
    upcased. Used to report a configuration problem in the name its owner set it under.
    """
    return env_var_names(field_name)[0]


def env_var_names(field_name: str) -> tuple[str, ...]:
    """Every name the environment may set a field under: `env_var_name`, then older ones.

    An older name (`JARVIS_PIN`) is only ever read: the store, the CLI and every message
    use the first.
    """
    field = Settings.model_fields.get(field_name)
    alias = getattr(field, "validation_alias", None) if field is not None else None
    if isinstance(alias, AliasChoices):
        return tuple(str(choice) for choice in alias.choices)
    return (alias if isinstance(alias, str) else field_name.upper(),)


def field_for(key: str) -> str | None:
    """The `Settings` field a key names (`KERYX_PIN` → `pin`), or None for no setting.

    An older name (`JARVIS_PIN`) names its field too, so a `.env` from before the rename
    imports, and `config set JARVIS_PIN` is refused as the PIN it is.
    """
    wanted = key.strip().upper()
    for name in Settings.model_fields:
        if wanted in env_var_names(name):
            return name
    return None


def canonical_key(key: str) -> str:
    """`key` as the store and every rule name it: an older name (`FAKE_AGENTS`) becomes the
    current one (`DEMO_MODE`), so no rule can be passed by typing the old one."""
    wanted = key.strip().upper()
    name = field_for(wanted)
    return env_var_name(name) if name is not None else wanted


def _extra(name: str) -> dict[str, Any]:
    extra = Settings.model_fields[name].json_schema_extra
    return extra if isinstance(extra, dict) else {}


def field_group(name: str) -> str:
    """The `GROUPS` key a field is listed under."""
    return str(_extra(name)["group"])


def field_title(name: str) -> str:
    """What a field is called where a person reads it rather than types it."""
    return str(_extra(name)["title"])


def field_tab(name: str) -> str | None:
    """The `TABS` key of the tab a field is shown on in the Mac app; None when a screen
    owns it (`field_screen`)."""
    return TAB_OF_GROUP.get(field_group(name))


def field_screen(name: str) -> str | None:
    """The `SCREENS` key of the app screen that owns a field, or None for one on a tab."""
    return SCREEN_OF_GROUP.get(field_group(name))


def field_advanced(name: str) -> bool:
    """Whether a field is shown under its tab's "Advanced" disclosure rather than first."""
    return bool(_extra(name)["advanced"])


def field_service_writable(name: str) -> bool:
    """What the field itself declares; the owner's overrides are `permissions`' business."""
    return bool(_extra(name).get("service_writable"))


def is_secret(name: str) -> bool:
    """A field declared `repr=False`: kept in `secrets.toml` and never printed."""
    return not Settings.model_fields[name].repr


def load_settings(**overrides: object) -> Settings:
    """Build a `Settings` instance, optionally overriding fields (used by tests/CLI)."""
    return Settings(**overrides)
