"""The voice server's speech models: which Hugging Face repositories speech-to-speech fetches
for each `--stt` and `--tts`, whether they are in the Hugging Face cache, and fetching them
ahead of the server's first start (`keryx voice download`, through `voice.download`).

The repositories are speech-to-speech's own, read from the source of the version Keryx
installs (`runtimes.S2S_VERSION`, 1.0.0) on 2026-10-07. Its handlers decide by
`sys.platform == "darwin"`, so this does too:

- `--stt parakeet-tdt` — `STT/parakeet_tdt_handler.py`: mlx-community/parakeet-tdt-0.6b-v3
  through mlx-audio on a Mac, nvidia/parakeet-tdt-0.6b-v3 through nano-parakeet elsewhere;
  `--parakeet_tdt_model_name` names another.
- `--stt whisper` — `arguments_classes/whisper_stt_arguments.py`: distil-whisper/
  distil-large-v3 (`--stt_model_name`), through transformers' `from_pretrained`
  (`STT/whisper_stt_handler.py`).
- `--tts kokoro` — `TTS/kokoro_handler.py`: mlx-community/Kokoro-82M-bf16 through mlx-audio
  on a Mac (`--kokoro_model_name`), whose `KokoroPipeline.load_single_voice` reads
  `voices/<name>.safetensors` from that repository, the handler preloading af_heart, ef_dora
  and ff_siwis beside the chosen voice (`KOKORO_LANG_DEFAULT_VOICES`, for "a", "e" and
  "f"); hexgrad/Kokoro-82M elsewhere, through kokoro's `KPipeline`, which takes no model name.
- `--tts qwen3` — `TTS/qwen3_tts_handler.py`: Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice
  (`--qwen3_tts_model_name`), mapped on a Mac to its mlx-community conversion at
  `--qwen3_tts_mlx_quantization`, 6bit by default (`_resolve_mlx_model_name`).
- every pipeline's turn detector — `VAD/smart_turn.py`: pipecat-ai/smart-turn-v3's
  smart-turn-v3.2-cpu.onnx, unless `--no_smart_turn` or `--smart_turn_model_path`.

On a Mac mlx-audio's `load_model` fetches its `DEFAULT_ALLOW_PATTERNS` (`mlx_audio/utils.py`),
which `MLX_FILES` copies. Where the library that loads a model is not part of the Mac install
(nano-parakeet, kokoro, faster-qwen3-tts), its files cannot be read off its source, so the
whole repository is fetched. Any other `--stt` or `--tts` — whisper-mlx is Lightning Whisper
MLX, an extra Keryx does not install, with repositories of its own — is one Keryx cannot name,
and says so rather than guess. A model named in `VOICE_SERVER_ARGS` is the one fetched.

A repository counts as downloaded when the files the server reads are all in the cache's
snapshot of it: every file of its tree listing (`trees/<commit>.json`, which
`snapshot_download` keeps) that its patterns take, and the files it `needs` — all there is to
ask of one fetched a file at a time, which leaves no listing.
"""

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from keryx.config import Settings
from keryx.localmodels.download import hf_cache

#: The platform speech-to-speech's handlers ask, unless a caller says another.
PLATFORM = sys.platform
#: mlx-audio's `DEFAULT_ALLOW_PATTERNS` (mlx_audio/utils.py): what its `load_model` fetches.
MLX_FILES = ("*.json", "*.safetensors", "*.py", "*.model", "*.tiktoken", "*.txt", "*.jinja",
             "*.jsonl", "*.yaml", "*.npz", "*.pth")
#: What transformers' `from_pretrained` reads of a model: its configurations and tokenizer
#: (`config.json`, `preprocessor_config.json`, `vocab.json`, `merges.txt`, …) and its
#: weights, `model.safetensors` or the shards its `.index.json` names.
TRANSFORMERS_FILES = ("*.json", "*.txt", "model.safetensors", "model-*-of-*.safetensors")
#: The voices mlx-audio's Kokoro preloads beside the chosen one.
KOKORO_PRELOADED = ("af_heart", "ef_dora", "ff_siwis")
SMART_TURN = "pipecat-ai/smart-turn-v3"
SMART_TURN_FILE = "smart-turn-v3.2-cpu.onnx"
QWEN3_TTS = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
QWEN3_QUANTIZATIONS = ("bf16", "4bit", "6bit", "8bit")
#: The script `fetch` runs with speech-to-speech's own Python.
FETCH_SCRIPT = Path(__file__).with_name("speech_fetch.py")
#: How long a stopped download is given to end before it is killed.
STOP_S = 5.0


class UnknownModels(ValueError):
    """A `--stt` or `--tts` whose models Keryx cannot name, in a sentence."""


@dataclass(frozen=True)
class Repo:
    """One Hugging Face repository: `allow` the patterns of it fetched (None: all of it), and
    `needs` the files that must be there whatever its listing says."""

    id: str
    allow: tuple[str, ...] | None = None
    needs: tuple[str, ...] = ()


# --- which repositories --------------------------------------------------------------------


def _words(args: str) -> list[str]:
    """`VOICE_SERVER_ARGS` as the server's command line splits it; none when it cannot."""
    try:
        return shlex.split(args)
    except ValueError:  # an unclosed quote: the server would not start on it either
        return []


def _flag(args: str, name: str) -> str | None:
    """The value `--name value` or `--name=value` gives in `args`, the last one winning."""
    words = _words(args)
    found = None
    for i, word in enumerate(words):
        if word == f"--{name}" and i + 1 < len(words):
            found = words[i + 1]
        elif word.startswith(f"--{name}="):
            found = word.split("=", 1)[1]
    return found


def _is_path(name: str) -> bool:
    return name.startswith((".", "/", "~"))


def _hub(name: str, allow: tuple[str, ...] | None, needs: tuple[str, ...] = ()) -> list[Repo]:
    """`name` as a repository to fetch; none when it is a path on this machine."""
    return [] if _is_path(name) else [Repo(name, allow, needs)]


def stt_repos(value: str, *, platform: str | None = None, args: str = "") -> list[Repo] | None:
    """What `--stt value` fetches, or None for one Keryx cannot name."""
    mac = (platform or PLATFORM) == "darwin"
    if value == "parakeet-tdt":
        default = "mlx-community/parakeet-tdt-0.6b-v3" if mac else "nvidia/parakeet-tdt-0.6b-v3"
        name = _flag(args, "parakeet_tdt_model_name") or default
        return _hub(name, MLX_FILES if mac else None)
    if value == "whisper":
        name = _flag(args, "stt_model_name") or "distil-whisper/distil-large-v3"
        return _hub(name, TRANSFORMERS_FILES)
    return None


def tts_repos(
    value: str, voice: str, *, platform: str | None = None, args: str = ""
) -> list[Repo] | None:
    """What `--tts value` fetches to speak in `voice`, or None for one Keryx cannot name."""
    mac = (platform or PLATFORM) == "darwin"
    if value == "kokoro":
        if not mac:
            return [Repo("hexgrad/Kokoro-82M")]
        voices = tuple(dict.fromkeys([voice, *KOKORO_PRELOADED]))
        name = _flag(args, "kokoro_model_name") or "mlx-community/Kokoro-82M-bf16"
        return _hub(name, MLX_FILES, tuple(f"voices/{v}.safetensors" for v in voices))
    if value == "qwen3":
        name = _flag(args, "qwen3_tts_model_name") or QWEN3_TTS
        if not mac:
            return _hub(name, None)
        return _hub(_qwen3_mlx(name, _flag(args, "qwen3_tts_mlx_quantization")), MLX_FILES)
    return None


def _qwen3_mlx(name: str, quantization: str | None) -> str:
    """`Qwen3TTSHandler._resolve_mlx_model_name`, with its quantization (6bit unless said)."""
    quant = (quantization if quantization is not None else "6bit").strip().lower()
    quant = None if quant in ("", "none", "default") else quant
    suffix = next((s for s in QWEN3_QUANTIZATIONS if name.endswith(f"-{s}")), None)
    if name.startswith("Qwen/"):
        name = name.replace("Qwen/", "mlx-community/", 1)
    elif not name.startswith("mlx-community/"):
        return name
    if quant is None:
        return name if suffix else f"{name}-6bit"
    base = name[: -len(suffix) - 1] if suffix else name
    return f"{base}-{quant}"


def turn_repos(args: str = "") -> list[Repo]:
    """The turn detector every pipeline loads, unless it is off or a file of the owner's."""
    if "--no_smart_turn" in _words(args) or _flag(args, "smart_turn_model_path"):
        return []
    return [Repo(SMART_TURN, (SMART_TURN_FILE,), (SMART_TURN_FILE,))]


def needed(settings: Settings, *, platform: str | None = None) -> list[Repo]:
    """Every repository the voice server here needs, as the settings choose it;
    `UnknownModels` naming the one Keryx cannot name."""
    args = settings.voice_server_args
    stt = stt_repos(settings.voice_server_stt, platform=platform, args=args)
    if stt is None:
        raise UnknownModels(_unknown("stt", settings.voice_server_stt))
    tts = tts_repos(settings.voice_server_tts, settings.voice_server_voice, platform=platform,
                    args=args)
    if tts is None:
        raise UnknownModels(_unknown("tts", settings.voice_server_tts))
    return [*stt, *tts, *turn_repos(args)]


def _unknown(kind: str, value: str) -> str:
    return (f"Keryx doesn't know which models speech-to-speech's --{kind} {value} needs, so "
            "it can't fetch them ahead: the voice server fetches them on its first start")


# --- whether they are in the cache ---------------------------------------------------------


def cached(repo: Repo, hub: Path | None = None) -> bool:
    """Whether every file of `repo` the server reads is in the cache's snapshot of it."""
    folder = (hub or hf_cache()) / ("models--" + repo.id.replace("/", "--"))
    try:
        commit = (folder / "refs" / "main").read_text().strip()
    except OSError:
        return False
    snapshot = folder / "snapshots" / commit
    wanted = list(repo.needs)
    listing = folder / "trees" / f"{commit}.json"
    if listing.is_file():
        try:
            files = json.loads(listing.read_text())["files"]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        wanted += [name for name in files
                   if repo.allow is None or any(fnmatch(name, p) for p in repo.allow)]
    # `exists` follows the link into `blobs/`: a link whose blob is gone is no file.
    return bool(wanted) and all((snapshot / name).exists() for name in wanted)


def _all_cached(repos: list[Repo] | None, hub: Path | None) -> bool:
    return repos is not None and all(cached(repo, hub) for repo in repos)


def ready(
    settings: Settings, kind: str, value: str, *, hub: Path | None = None,
    platform: str | None = None,
) -> bool:
    """Whether `value` for `VOICE_SERVER_STT` (`kind` "stt") or `_TTS` ("tts") has its
    models in the cache: what `keryx config options` marks "downloaded"."""
    args = settings.voice_server_args
    if kind == "stt":
        repos = stt_repos(value, platform=platform, args=args)
    else:
        repos = tts_repos(value, settings.voice_server_voice, platform=platform, args=args)
    return _all_cached(repos, hub)


def models_ready(settings: Settings, *, hub: Path | None = None,
                 platform: str | None = None) -> bool:
    """Whether every model the voice server here needs is in the cache."""
    try:
        repos = needed(settings, platform=platform)
    except UnknownModels:
        return False
    return _all_cached(repos, hub)


# --- fetching them -------------------------------------------------------------------------


def fetch(
    python: str,
    repos: Sequence[Repo],
    progress: Callable[[int, int | None], None],
    *,
    started: Callable[[subprocess.Popen], None] = lambda child: None,
) -> str | None:
    """Fetch `repos` into the Hugging Face cache with speech-to-speech's own `python`
    (`speech_fetch.py`), `progress` told the bytes on disk; None, or why it stopped.

    Its output is read line by line; its stderr is this process's. However this ends — a
    Cancel included — the download is not left running behind it."""
    spec = json.dumps([{"id": r.id, "allow": list(r.allow) if r.allow else None}
                       for r in repos])
    try:
        child = subprocess.Popen([python, str(FETCH_SCRIPT), spec], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, text=True, env=dict(os.environ))
    except OSError as error:
        return f"could not run speech-to-speech's Python ({python}): {error.strerror or error}"
    started(child)
    said = None
    try:
        assert child.stdout is not None
        for line in child.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if "progress" in event:
                done, total = event["progress"]
                progress(done, total)
            elif "error" in event:
                said = str(event["error"])
        code = child.wait()
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(STOP_S)
            except subprocess.TimeoutExpired:  # pragma: no cover - one that ignores SIGTERM
                child.kill()
                child.wait()
        if child.stdout is not None:
            child.stdout.close()
    if code == 0:
        return None
    return said or f"the download stopped (exit {code})"
