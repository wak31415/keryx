"""The values a setting can take, found when the owner asks: `keryx config options KEY`.

The Mac app fills a dropdown from this the moment it is opened, so the list is read live
where there is something to ask — OpenAI's and Anthropic's model lists, Codex's own, a
server of the owner's — and built in where there is not. Each source is one small function
here; whatever reaches outside the process (the HTTP getter, Codex, the hardware and the
disk) comes in through `Sources`, which the tests replace wholesale.

A list that cannot be read is never a failure of the command: it falls back to the values
Keryx knows, `live` false and `error` one sentence saying why, and only why (the app says
what it shows instead) — an exception's type or a status code at most, never a response
body, which can quote the key back. A key goes in a request header and
nowhere else: not in an error, not in a log line, not in what is printed. Every request is
a `GET` with a five-second timeout that follows no redirect.

`live` is true only when something outside Keryx answered just now; the catalog, read from
this machine's disk and hardware, is not live and needs no error. The value set now is
always among the options, with the detail "current", so the dropdown can show it.
"""

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

import httpx

from keryx.config.settings import Settings, field_for
from keryx.endpoints import Endpoint, probe
from keryx.localmodels.catalog import MODELS, VOICES, ModelEntry, fits
from keryx.localmodels.hardware import Hardware
from keryx.persona import PERSONAS

#: How long any one source may take: the owner is holding a dropdown open.
TIMEOUT_S = 5.0
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
#: Anthropic pages by 20 unless asked; 1000 is its largest page, so one request is all of it.
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models?limit=1000"
ANTHROPIC_VERSION = "2023-06-01"

#: The Realtime API's voices. `PERSONAS` names the ones the built-in assistants speak in.
REALTIME_VOICES = (
    "alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse",
)
#: speech-to-speech's `--stt` and `--tts` handlers Keryx can set up: the ones whose models
#: `keryx voice download` can name and fetch (`localmodels.speech`). The command takes
#: others, so the field is free-form and a value set before is kept; these are the ones
#: offered. Not whisper-mlx: Lightning Whisper MLX is an extra Keryx does not install, with
#: models of its own package's choosing.
VOICE_SERVER_STT = ("parakeet-tdt", "whisper")
VOICE_SERVER_TTS = ("kokoro", "qwen3")
#: What an OpenAI id holds when it is not a chat model the Responses API can search with.
NOT_CHAT = (
    "realtime", "audio", "transcribe", "tts", "image", "search-preview", "embedding",
    "moderation",
)

Source = Literal["openai", "anthropic", "codex", "server", "catalog", "builtin"]


class Unavailable(Exception):
    """Why a live list could not be read: one sentence of cause and nothing else — the app
    says itself that it is showing the built-in list — in sentence case, ending with a full
    stop, never quoting a key or a response body."""


class CodexModel(NamedTuple):
    id: str
    display_name: str
    description: str


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    detail: str | None = None


@dataclass(frozen=True)
class Options:
    """What `keryx config options KEY --json` prints; the field names are a contract."""

    key: str
    current: str | None
    options: list[Option]
    source: Source
    live: bool
    free_form: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "current": self.current,
            "options": [
                {"value": o.value, "label": o.label, "detail": o.detail} for o in self.options
            ],
            "source": self.source,
            "live": self.live,
            "free_form": self.free_form,
            "error": self.error,
        }


@dataclass(frozen=True)
class Sources:
    """Everything outside this process a list is read from."""

    #: `httpx.get`'s shape: `get(url, *, headers, timeout, follow_redirects)`.
    get: Callable[..., Any]
    #: `(environment, timeout)` → Codex's models; raises `Unavailable`.
    codex_models: Callable[[dict[str, str], float], list[CodexModel]]
    hardware: Callable[[], Hardware]
    downloaded: Callable[[ModelEntry], bool]
    #: `(kind, value)` → whether `VOICE_SERVER_STT` ("stt") or `_TTS` ("tts") at `value`
    #: has its speech models in the Hugging Face cache (`localmodels.speech.ready`).
    speech_ready: Callable[[str, str], bool] = lambda kind, value: False


@dataclass(frozen=True)
class Found:
    options: list[Option]
    source: Source
    live: bool
    error: str | None = None


# --- the real sources ----------------------------------------------------------------------


def http_get(url: str, **kwargs: Any) -> httpx.Response:
    """`httpx.get` that never follows a redirect, whatever it is asked: a redirect could
    carry the key's header to another host."""
    return httpx.get(url, **{**kwargs, "follow_redirects": False})


def sdk_codex_models(env: dict[str, str], timeout: float) -> list[CodexModel]:
    """The models Codex lists, from an app-server started for the asking.

    It answers from its own catalog in under a second, signed in or not (measured
    2026-10-07), so it is cheap enough to ask each time a dropdown opens. The thread is a
    daemon so a hung app-server cannot hold the command past `timeout`, and the client it
    started is published as soon as it exists, so on a timeout the caller closes it — which
    terminates the `codex app-server` child, and kills it two seconds later.
    """
    try:
        from openai_codex import Codex, CodexConfig
    except ImportError:
        raise Unavailable("The Codex agent isn't installed.") from None
    result: list[CodexModel] = []
    failure: list[BaseException] = []
    started: list[Any] = []

    def ask() -> None:
        try:
            config = CodexConfig(env=env, cwd="/", client_name="keryx", client_title="Keryx")
            codex = Codex(config)
            started.append(codex)
            try:
                listed = codex.models()
            finally:
                codex.close()
            result.extend(
                CodexModel(m.id, m.display_name, m.description)
                for m in listed.data
                if not m.hidden
            )
        except BaseException as exc:  # reported by its type only: its text may quote a key
            failure.append(exc)

    worker = threading.Thread(target=ask, name="codex-models", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        for codex in started:
            with contextlib.suppress(Exception):
                codex.close()
        raise Unavailable(f"Codex didn't list its models within {timeout:g} seconds.")
    if failure:
        raise Unavailable(f"Codex couldn't list its models ({type(failure[0]).__name__}).")
    return result


def default_sources(settings: Settings) -> Sources:
    """The real network, Codex, hardware and disk."""

    def hardware() -> Hardware:
        from keryx.localmodels import hardware as detect

        return detect.detect()

    def downloaded(entry: ModelEntry) -> bool:
        from keryx.localmodels import download

        return download.is_downloaded(settings.cache_dir, entry)

    def speech_ready(kind: str, value: str) -> bool:
        from keryx.localmodels import speech

        return speech.ready(settings, kind, value)

    return Sources(
        get=http_get, codex_models=sdk_codex_models, hardware=hardware, downloaded=downloaded,
        speech_ready=speech_ready,
    )


# --- reading a list ------------------------------------------------------------------------


def _get_json(get: Callable[..., Any], url: str, headers: dict[str, str], provider: str) -> Any:
    """`GET url` as JSON, or `Unavailable` naming the provider and at most a status code."""
    try:
        response = get(url, headers=headers, timeout=TIMEOUT_S, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise Unavailable(f"Keryx couldn't reach {provider} ({type(exc).__name__}).") from None
    if response.status_code in (401, 403):
        raise Unavailable(f"{provider} didn't accept the API key.")
    if response.status_code != 200:
        raise Unavailable(f"{provider} answered with HTTP {response.status_code}.")
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise Unavailable(f"{provider} didn't answer with a list of models.")
    return body


def _live(source: Source, read: Callable[[], list[Option]], fallback: list[Option]) -> Found:
    try:
        return Found(read(), source, True)
    except Unavailable as why:
        return Found(fallback, "builtin", False, str(why))
    except Exception as exc:  # by its type only: its text may quote a key or a body
        return Found(fallback, "builtin", False, _unreadable(exc))


def _unreadable(exc: Exception) -> str:
    return f"Keryx couldn't read the list ({type(exc).__name__})."


def _no_key(missing: str) -> str:
    """Why nothing could be asked: `missing`, said as a cause and nothing else."""
    return f"{missing}, so Keryx can't ask which models exist."


def _default(field: str) -> str:
    return str(Settings.model_fields[field].default)


def _ids(field: str, ids: list[str]) -> list[Option]:
    default = _default(field)
    return [Option(i, i, "Keryx's default" if i == default else None) for i in ids]


def _openai(
    field: str, family: str, keep: Callable[[str], bool]
) -> Callable[[Settings, Sources], Found]:
    """A provider for one `family` of OpenAI's models, newest-looking id first."""

    def provide(settings: Settings, sources: Sources) -> Found:
        def read() -> list[Option]:
            key = settings.openai_key
            if not key:
                raise Unavailable(_no_key("There's no OpenAI API key"))
            body = _get_json(
                sources.get, OPENAI_MODELS_URL, {"Authorization": f"Bearer {key}"}, "OpenAI"
            )
            ids = sorted(
                {str(m["id"]) for m in body["data"] if isinstance(m, dict) and m.get("id")},
                reverse=True,
            )
            found = [i for i in ids if keep(i)]
            if not found:
                raise Unavailable(f"OpenAI lists no {family} models.")
            return _ids(field, found)

        return _live("openai", read, _ids(field, [_default(field)]))

    return provide


def _server(endpoint: Endpoint | None, get: Callable[..., Any], missing: str) -> Found:
    """A server of the owner's own, asked for its models; only the current value without.

    `endpoints.probe` reads the answer (both servers' shapes); what happened on the wire is
    noted on the way through, so the error can say it in this module's own sentences.
    """

    def read() -> list[Option]:
        if endpoint is None:
            raise Unavailable(missing)
        seen: dict[str, Any] = {}

        def noting(url: str, **kwargs: Any) -> Any:
            kwargs.update(timeout=TIMEOUT_S, follow_redirects=False)
            try:
                response = get(url, **kwargs)
            except httpx.HTTPError as exc:
                seen["error"] = type(exc).__name__
                raise
            seen["status"] = response.status_code
            return response

        said = probe(endpoint, get=noting)
        host, status = endpoint.host, seen.get("status")
        if not said.ok:
            if "error" in seen:
                raise Unavailable(f"Keryx couldn't reach the server at {host} ({seen['error']}).")
            if status in (401, 403):
                raise Unavailable(f"The server at {host} didn't accept the API key.")
            if status != 200:
                raise Unavailable(f"The server at {host} answered with HTTP {status}.")
            raise Unavailable(f"The server at {host} didn't answer with a list of models.")
        if not said.models:
            raise Unavailable(f"The server at {host} lists no models.")
        return [Option(model, model) for model in said.models]

    return _live("server", read, [])


# --- one provider per setting --------------------------------------------------------------

#: Realtime models that do not hold a conversation: they transcribe or translate speech.
NOT_VOICE = ("whisper", "translate", "transcribe", "audio-preview")
_openai_realtime = _openai(
    "openai_realtime_model",
    "Realtime voice",
    lambda i: "realtime" in i and not any(word in i for word in NOT_VOICE),
)


def realtime_models(settings: Settings, sources: Sources) -> Found:
    """OpenAI's Realtime models — or, with `VOICE_BASE_URL`, that server's, since an OpenAI
    id offered for a server of the owner's own would leave every call silent."""
    if settings.voice_base_url:
        return _server(settings.voice_endpoint, sources.get, "")
    return _openai_realtime(settings, sources)


transcription_models = _openai(
    "openai_transcription_model",
    "transcription",
    lambda i: "transcribe" in i or i.startswith("whisper"),
)
web_search_models = _openai(
    "openai_web_search_model",
    "chat",
    lambda i: i.startswith("gpt-") and not any(word in i for word in NOT_CHAT),
)


def realtime_voices(settings: Settings, sources: Sources) -> Found:
    """The Realtime voices; blank is the assistant's own, which the persona chooses."""
    owners = {persona.voice: persona.name for persona in PERSONAS.values()}
    voices = [
        Option(voice, voice.capitalize(), f"{owners[voice]}'s voice" if voice in owners else None)
        for voice in REALTIME_VOICES
    ]
    return Found([Option("", "The assistant's own voice"), *voices], "builtin", False)


def _aliased(models: dict[str, str], label: Callable[[str, str], str]) -> list[Option]:
    return [Option(model, label(alias, model), alias) for alias, model in models.items()]


def claude_models(settings: Settings, sources: Sources) -> Found:
    """Anthropic's list, which it gives only to an API key; the aliases Keryx knows else."""
    from keryx.agents.claude import CLAUDE_MODELS

    known = _aliased(CLAUDE_MODELS, lambda alias, _: f"Claude {alias.capitalize()}")
    aliases = {model: alias for alias, model in CLAUDE_MODELS.items()}

    def read() -> list[Option]:
        key = settings.anthropic_api_key
        if not key:
            raise Unavailable(_no_key("There's no Anthropic API key"))
        body = _get_json(
            sources.get,
            ANTHROPIC_MODELS_URL,
            {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
            "Anthropic",
        )
        found = [
            Option(str(m["id"]), str(m.get("display_name") or m["id"]), aliases.get(m["id"]))
            for m in body["data"]
            if isinstance(m, dict) and str(m.get("id", "")).startswith("claude-")
        ]
        if not found:
            raise Unavailable("Anthropic lists no Claude models.")
        return found

    return _live("anthropic", read, known)


def _codex_label(model: str) -> str:
    """`gpt-6-astra` as Codex itself writes it: `GPT-6-Astra`."""
    parts = model.split("-")
    return "-".join(part.upper() if part == "gpt" else part.capitalize() for part in parts)


def _codex_env(settings: Settings) -> dict[str, str]:
    """The environment a Codex task would run under, without ever logging in: a dropdown
    must not be what spends a login. Every credential variable is blanked but the one in
    use, as `CodexAgentRunner` does, so `OPENAI_API_KEY` is never lent to Codex."""
    from keryx.agents.auth import AuthMode, resolve_auth
    from keryx.agents.codex import CODEX_AUTH, CREDENTIAL_VARIABLES, private_codex_home

    auth = resolve_auth(CODEX_AUTH, settings, probe=False)
    env = dict.fromkeys(CREDENTIAL_VARIABLES, "")
    if auth.mode is AuthMode.API_KEY:
        home = private_codex_home(settings)
        if not (home / "auth.json").exists():
            raise Unavailable("Codex hasn't logged in with CODEX_API_KEY yet.")
        env["CODEX_HOME"] = str(home)
    elif auth.mode is AuthMode.TOKEN:
        env["CODEX_ACCESS_TOKEN"] = auth.secret or ""
    return env


#: A blank `CODEX_MODEL` is Codex choosing. `SUBAGENT_MODEL` has no such option: it is a
#: plain string with a default, and blank names no model at all.
CODEX_DEFAULT = Option("", "Codex's default")


def codex_models(settings: Settings, sources: Sources) -> Found:
    """What Codex lists for the way its tasks sign in; the aliases Keryx knows else."""
    from keryx.agents.codex import CODEX_MODELS

    def read() -> list[Option]:
        listed = sources.codex_models(_codex_env(settings), TIMEOUT_S)
        if not listed:
            raise Unavailable("Codex lists no models.")
        return [
            CODEX_DEFAULT,
            *(Option(m.id, m.display_name or m.id, m.description or None) for m in listed),
        ]

    known = _aliased(CODEX_MODELS, lambda _, model: _codex_label(model))
    return _live("codex", read, [CODEX_DEFAULT, *known])


def local_agent_models(settings: Settings, sources: Sources) -> Found:
    return _server(
        settings.local_agent_endpoint,
        sources.get,
        _no_key("There's no local agent server address"),
    )


def served_models(settings: Settings, sources: Sources) -> Found:
    """The catalog: what is downloaded first, then whether the rest would fit."""
    hardware = sources.hardware()
    machine = "this Mac" if hardware.accelerator == "metal" else "this machine"
    ready, rest = [], []
    for entry in MODELS:
        if sources.downloaded(entry):
            ready.append(Option(entry.key, entry.title, "downloaded"))
        else:
            detail = "fits" if fits(entry, hardware) else f"too big for {machine}"
            rest.append(Option(entry.key, entry.title, detail))
    return Found([*ready, *rest], "catalog", False)


def kokoro_voices(settings: Settings, sources: Sources) -> Found:
    return Found(
        [Option(name, name.split("_", 1)[-1].capitalize(), sounds) for name, sounds in VOICES],
        "catalog",
        False,
    )


def _speech(kind: str, values: tuple[str, ...]) -> Callable[[Settings, Sources], Found]:
    """The voice server's speech models Keryx offers, each marked "downloaded" once its
    models are in the Hugging Face cache (`keryx voice download` fetches them)."""
    def found(settings: Settings, sources: Sources) -> Found:
        return Found([
            Option(v, v, "downloaded" if sources.speech_ready(kind, v) else None)
            for v in values
        ], "builtin", False)

    return found


PROVIDERS: dict[str, Callable[[Settings, Sources], Found]] = {
    "OPENAI_REALTIME_MODEL": realtime_models,
    "OPENAI_TRANSCRIPTION_MODEL": transcription_models,
    "OPENAI_WEB_SEARCH_MODEL": web_search_models,
    "OPENAI_VOICE": realtime_voices,
    "SUBAGENT_MODEL": claude_models,
    "CODEX_MODEL": codex_models,
    "LOCAL_AGENT_MODEL": local_agent_models,
    "LLM_SERVER_MODEL": served_models,
    "VOICE_SERVER_VOICE": kokoro_voices,
    "VOICE_SERVER_STT": _speech("stt", VOICE_SERVER_STT),
    "VOICE_SERVER_TTS": _speech("tts", VOICE_SERVER_TTS),
}
OPTION_KEYS = frozenset(PROVIDERS)
#: The one setting whose value must be on the list: a voice the key may not use makes
#: every call fail to open.
CLOSED = frozenset({"OPENAI_VOICE"})


def has_options(key: str) -> bool:
    return key in OPTION_KEYS


def options_for(key: str, settings: Settings, sources: Sources | None = None) -> Options:
    """The options for `KEY` (its environment name). `KeyError` for a key without any."""
    provide = PROVIDERS[key]
    try:
        found = provide(settings, sources or default_sources(settings))
    except Exception as exc:  # the last line under "exit 0 for every supported key"
        found = Found([], "builtin", False, _unreadable(exc))
    name = field_for(key)
    assert name is not None
    raw = getattr(settings, name)
    current = None if raw is None else str(raw)
    listed = list(found.options)
    values = {option.value for option in listed}
    if not current and "" in values:
        current = ""  # unset is the blank option — Codex's default, the assistant's voice
    elif current and current not in values:
        listed.append(Option(current, current, "current"))
    return Options(
        key, current, listed, found.source, found.live, key not in CLOSED, found.error
    )
