"""The voice server's speech models: the Hugging Face repositories speech-to-speech 1.0.0
fetches for each `--stt` and `--tts` (read from its source), whether they are in the cache,
and the download run inside its own environment — never Hugging Face itself."""

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from keryx.config import Settings
from keryx.localmodels import speech
from keryx.localmodels.speech import MLX_FILES, TRANSFORMERS_FILES, Repo

MAC, LINUX = "darwin", "linux"


def ids(repos) -> list[str]:
    return [repo.id for repo in repos]


# --- which repositories -----------------------------------------------------------------


@pytest.mark.parametrize(("value", "platform", "repo", "files"), [
    # STT/parakeet_tdt_handler.py: MLX on a Mac, nano-parakeet elsewhere.
    ("parakeet-tdt", MAC, "mlx-community/parakeet-tdt-0.6b-v3", MLX_FILES),
    ("parakeet-tdt", LINUX, "nvidia/parakeet-tdt-0.6b-v3", None),
    # arguments_classes/whisper_stt_arguments.py: transformers, everywhere.
    ("whisper", MAC, "distil-whisper/distil-large-v3", TRANSFORMERS_FILES),
    ("whisper", LINUX, "distil-whisper/distil-large-v3", TRANSFORMERS_FILES),
])
def test_each_stt_is_the_repository_speech_to_speech_loads(value, platform, repo, files):
    [found] = speech.stt_repos(value, platform=platform)

    assert found == Repo(repo, files)


@pytest.mark.parametrize(("value", "platform", "repo", "files"), [
    # TTS/kokoro_handler.py: mlx-audio's conversion on a Mac, kokoro's KPipeline elsewhere.
    ("kokoro", MAC, "mlx-community/Kokoro-82M-bf16", MLX_FILES),
    ("kokoro", LINUX, "hexgrad/Kokoro-82M", None),
    # TTS/qwen3_tts_handler.py: DEFAULT_MLX_MODEL on a Mac, DEFAULT_MODEL elsewhere.
    ("qwen3", MAC, "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit", MLX_FILES),
    ("qwen3", LINUX, "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", None),
])
def test_each_tts_is_the_repository_speech_to_speech_loads(value, platform, repo, files):
    [found] = speech.tts_repos(value, "af_heart", platform=platform)

    assert (found.id, found.allow) == (repo, files)


def test_kokoro_on_a_mac_needs_its_voice_and_the_ones_it_preloads():
    """mlx-audio's KokoroPipeline loads `voices/<name>.safetensors` from the model's own
    repository, and the handler preloads American, Spanish and French voices beside it."""
    [kokoro] = speech.tts_repos("kokoro", "bm_george", platform=MAC)

    assert kokoro.needs == ("voices/bm_george.safetensors", "voices/af_heart.safetensors",
                            "voices/ef_dora.safetensors", "voices/ff_siwis.safetensors")
    [preloaded] = speech.tts_repos("kokoro", "af_heart", platform=MAC)
    assert preloaded.needs.count("voices/af_heart.safetensors") == 1


def test_mlx_audio_fetches_its_default_files_and_transformers_its_weights():
    # mlx_audio/utils.py DEFAULT_ALLOW_PATTERNS, which its `load_model` downloads.
    assert MLX_FILES == ("*.json", "*.safetensors", "*.py", "*.model", "*.tiktoken", "*.txt",
                         "*.jinja", "*.jsonl", "*.yaml", "*.npz", "*.pth")
    assert "model.safetensors" in TRANSFORMERS_FILES and "*.json" in TRANSFORMERS_FILES


def test_every_pipeline_needs_its_turn_detector():
    """VAD/smart_turn.py: on unless --no_smart_turn, and a path of the owner's is no
    download at all."""
    assert speech.turn_repos("") == [
        Repo("pipecat-ai/smart-turn-v3", ("smart-turn-v3.2-cpu.onnx",),
             ("smart-turn-v3.2-cpu.onnx",))]
    assert speech.turn_repos("--no_smart_turn") == []
    assert speech.turn_repos("--smart_turn_model_path /models/turn.onnx") == []


@pytest.mark.parametrize(("args", "platform", "expected"), [
    ("--stt_model_name openai/whisper-base", MAC, "openai/whisper-base"),
    ("--parakeet_tdt_model_name=nvidia/parakeet-tdt-1.1b", LINUX, "nvidia/parakeet-tdt-1.1b"),
])
def test_a_model_named_in_the_extra_flags_is_the_one_fetched(args, platform, expected):
    value = "whisper" if "stt_model_name" in args else "parakeet-tdt"

    assert ids(speech.stt_repos(value, platform=platform, args=args)) == [expected]


@pytest.mark.parametrize(("args", "expected"), [
    ("--qwen3_tts_model_name Qwen/Qwen3-TTS-12Hz-0.6B-Base",
     "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-6bit"),
    ("--qwen3_tts_model_name mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
     "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-6bit"),
    ("--qwen3_tts_mlx_quantization 8bit", "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"),
    ("--qwen3_tts_mlx_quantization none --qwen3_tts_model_name mlx-community/X-bf16",
     "mlx-community/X-bf16"),
    ("--qwen3_tts_mlx_quantization none --qwen3_tts_model_name Qwen/X", "mlx-community/X-6bit"),
    ("--qwen3_tts_model_name /models/qwen3", None),
])
def test_qwen3_on_a_mac_is_mapped_as_its_handler_maps_it(args, expected):
    """`_resolve_mlx_model_name` with `qwen3_tts_mlx_quantization`, "6bit" by default; a
    local path is no download."""
    found = speech.tts_repos("qwen3", "af_heart", platform=MAC, args=args)

    assert ids(found) == ([expected] if expected else [])


def test_extra_flags_that_cannot_be_split_name_nothing():
    """An unclosed quote: the server would not start on it, and nothing here breaks."""
    assert ids(speech.stt_repos("whisper", platform=MAC, args="--stt_model_name 'x")) == [
        "distil-whisper/distil-large-v3"]
    assert ids(speech.turn_repos("--no_smart_turn '")) == ["pipecat-ai/smart-turn-v3"]


def test_kokoro_elsewhere_takes_no_model_name():
    """kokoro's KPipeline is built with a language and nothing else."""
    found = speech.tts_repos("kokoro", "af_heart", platform=LINUX,
                             args="--kokoro_model_name someone/else")

    assert ids(found) == ["hexgrad/Kokoro-82M"]


@pytest.mark.parametrize("value", ["whisper-mlx", "faster-whisper", "mine"])
def test_a_speech_model_keryx_cannot_name_is_none(value):
    assert speech.stt_repos(value, platform=MAC) is None
    assert speech.tts_repos(value, "af_heart", platform=MAC) is None


def test_needed_is_what_the_settings_choose(settings):
    chosen = settings.model_copy(update={"voice_server_stt": "whisper",
                                         "voice_server_tts": "kokoro",
                                         "voice_server_voice": "bf_emma"})

    found = speech.needed(chosen, platform=MAC)

    assert ids(found) == ["distil-whisper/distil-large-v3", "mlx-community/Kokoro-82M-bf16",
                          "pipecat-ai/smart-turn-v3"]
    assert "voices/bf_emma.safetensors" in found[1].needs


def test_needed_says_which_model_it_cannot_name(settings):
    with pytest.raises(speech.UnknownModels, match="--stt whisper-mlx"):
        speech.needed(settings.model_copy(update={"voice_server_stt": "whisper-mlx"}),
                      platform=MAC)
    with pytest.raises(speech.UnknownModels, match="--tts mine"):
        speech.needed(settings.model_copy(update={"voice_server_tts": "mine"}), platform=MAC)


# --- whether they are in the cache --------------------------------------------------------


def cache(hub: Path, repo: str, files: dict[str, int], *, listed: dict[str, int] | None = None,
          commit: str = "abc123") -> Path:
    """A repository in a Hugging Face cache, as snapshot_download leaves it: `refs/main`, the
    commit's tree listing (`trees/<commit>.json`, every file of the repository) when
    `listed`, and `files` linked into the snapshot from `blobs/`."""
    folder = hub / ("models--" + repo.replace("/", "--"))
    (folder / "refs").mkdir(parents=True, exist_ok=True)
    (folder / "refs" / "main").write_text(commit)
    if listed is not None:
        (folder / "trees").mkdir(exist_ok=True)
        (folder / "trees" / f"{commit}.json").write_text(json.dumps({
            "format_version": 1,
            "files": {name: {"size": size} for name, size in listed.items()},
        }))
    for name, size in files.items():
        blob = folder / "blobs" / f"blob-{name.replace('/', '-')}"
        blob.parent.mkdir(exist_ok=True)
        blob.write_bytes(b"\0" * size)
        link = folder / "snapshots" / commit / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(blob, link.parent))
    return folder


PARAKEET = {".gitattributes": 1, "README.md": 1, "config.json": 2, "model.safetensors": 9,
            "tokenizer.model": 3, "tokenizer.vocab": 1, "vocab.txt": 4}
#: What mlx-audio fetched of it, on the owner's Mac (2026-10-07): its patterns, no more.
PARAKEET_FETCHED = {"config.json": 2, "model.safetensors": 9, "tokenizer.model": 3,
                    "vocab.txt": 4}


def test_a_repository_is_cached_when_every_file_it_reads_is_in_the_snapshot(tmp_path):
    repo = Repo("mlx-community/parakeet-tdt-0.6b-v3", MLX_FILES)
    cache(tmp_path, repo.id, PARAKEET_FETCHED, listed=PARAKEET)

    assert speech.cached(repo, tmp_path) is True


def test_a_download_cut_off_is_not_cached(tmp_path):
    repo = Repo("mlx-community/parakeet-tdt-0.6b-v3", MLX_FILES)
    fetched = dict(PARAKEET_FETCHED)
    del fetched["model.safetensors"]
    cache(tmp_path, repo.id, fetched, listed=PARAKEET)

    assert speech.cached(repo, tmp_path) is False


def test_a_link_whose_blob_is_gone_is_not_cached(tmp_path):
    repo = Repo("mlx-community/parakeet-tdt-0.6b-v3", MLX_FILES)
    folder = cache(tmp_path, repo.id, PARAKEET_FETCHED, listed=PARAKEET)
    (folder / "blobs" / "blob-model.safetensors").unlink()

    assert speech.cached(repo, tmp_path) is False


def test_a_whole_repository_wants_every_file_listed(tmp_path):
    repo = Repo("hexgrad/Kokoro-82M")
    cache(tmp_path, repo.id, {"config.json": 1}, listed={"config.json": 1, "kokoro.pth": 5})
    assert speech.cached(repo, tmp_path) is False

    cache(tmp_path, repo.id, {"kokoro.pth": 5})
    assert speech.cached(repo, tmp_path) is True


def test_a_file_fetched_alone_counts_by_the_files_it_needs(tmp_path):
    """hf_hub_download leaves no tree listing: what must be there is all there is to ask."""
    [turn] = speech.turn_repos("")
    assert speech.cached(turn, tmp_path) is False

    cache(tmp_path, turn.id, {"smart-turn-v3.2-cpu.onnx": 8})
    assert speech.cached(turn, tmp_path) is True
    assert speech.cached(Repo("someone/listless"), tmp_path) is False


def test_a_voice_file_missing_is_not_cached(tmp_path):
    [kokoro] = speech.tts_repos("kokoro", "bf_emma", platform=MAC)
    voices = {f"voices/{name}.safetensors": 1 for name in ("af_heart", "ef_dora", "ff_siwis")}
    files = {"config.json": 1, "kokoro-v1_0.safetensors": 4, **voices}
    cache(tmp_path, kokoro.id, files, listed=files)

    assert speech.cached(kokoro, tmp_path) is False


def test_nothing_in_the_cache_is_nothing_cached(tmp_path):
    assert speech.cached(Repo("mlx-community/Kokoro-82M-bf16", MLX_FILES), tmp_path) is False
    folder = tmp_path / "models--a--b"
    (folder / "refs").mkdir(parents=True)
    (folder / "refs" / "main").write_text("gone")
    assert speech.cached(Repo("a/b"), tmp_path) is False
    (folder / "trees").mkdir()
    (folder / "trees" / "gone.json").write_text("not json")
    assert speech.cached(Repo("a/b"), tmp_path) is False


def ready_settings(stt="parakeet-tdt", tts="kokoro") -> Settings:
    return Settings(_env_file=None, openai_api_key="t", voice_server_stt=stt,
                    voice_server_tts=tts)


def fill(hub: Path, repos: list[Repo]) -> None:
    for repo in repos:
        files = {name: 1 for name in (*repo.needs, "config.json")}
        cache(hub, repo.id, files, listed=files)


def test_models_ready_is_every_model_the_settings_need(tmp_path):
    settings = ready_settings()
    assert speech.models_ready(settings, hub=tmp_path, platform=MAC) is False

    fill(tmp_path, speech.needed(settings, platform=MAC))
    assert speech.models_ready(settings, hub=tmp_path, platform=MAC) is True
    assert speech.models_ready(ready_settings(stt="whisper-mlx"), hub=tmp_path,
                               platform=MAC) is False


def test_ready_asks_one_setting_its_value(tmp_path):
    settings = ready_settings()
    fill(tmp_path, speech.stt_repos("parakeet-tdt", platform=MAC))

    assert speech.ready(settings, "stt", "parakeet-tdt", hub=tmp_path, platform=MAC) is True
    assert speech.ready(settings, "stt", "whisper", hub=tmp_path, platform=MAC) is False
    assert speech.ready(settings, "tts", "kokoro", hub=tmp_path, platform=MAC) is False
    assert speech.ready(settings, "stt", "whisper-mlx", hub=tmp_path, platform=MAC) is False
    fill(tmp_path, speech.tts_repos("kokoro", settings.voice_server_voice, platform=MAC))
    assert speech.ready(settings, "tts", "kokoro", hub=tmp_path, platform=MAC) is True


def test_the_cache_asked_by_default_is_hugging_faces(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    settings = ready_settings()
    fill(tmp_path, speech.needed(settings, platform=sys.platform))

    assert speech.models_ready(settings) is True


# --- the download, run by speech-to-speech's own Python ------------------------------------


FAKE_HUB = '''
"""A huggingface_hub that never reaches the network: what a test asks it is in FAKE_HUB_PLAN."""
import json, os, time
from pathlib import Path

PLAN = json.loads(os.environ["FAKE_HUB_PLAN"])


class Info:
    def __init__(self, name, size, local):
        self.filename, self.file_size, self.local_path = name, size, local
        self.is_cached = Path(local).exists()
        self.will_download = not self.is_cached


def snapshot_download(repo_id, *, allow_patterns=None, dry_run=False):
    files = PLAN["repos"][repo_id]
    root = Path(PLAN["hub"]) / repo_id
    if dry_run:
        return [Info(name, size, str(root / name)) for name, size in files.items()]
    if PLAN.get("fail") == repo_id:
        raise OSError("disk on fire")
    if PLAN.get("hang"):
        time.sleep(30)
    for name, size in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"\\0" * size)
    return str(root)
'''


@pytest.fixture
def fake_hub(tmp_path, monkeypatch):
    """`huggingface_hub` on the child's path, faked, and what it serves."""
    package = tmp_path / "fakes" / "huggingface_hub"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(textwrap.dedent(FAKE_HUB))
    (package / "constants.py").write_text(f"HF_HUB_CACHE = {str(tmp_path / 'hub')!r}\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "fakes"))

    def plan(**given):
        monkeypatch.setenv("FAKE_HUB_PLAN", json.dumps({"hub": str(tmp_path / "hub"), **given}))

    return plan


def test_fetch_runs_the_script_in_the_tools_python_and_hands_on_its_progress(fake_hub):
    fake_hub(repos={"a/one": {"config.json": 10, "model.safetensors": 90}, "b/two": {"x": 100}})
    seen: list[tuple] = []

    problem = speech.fetch(sys.executable, [Repo("a/one", MLX_FILES), Repo("b/two")],
                           lambda done, total: seen.append((done, total)))

    assert problem is None
    assert seen[0] == (0, 200) and seen[-1] == (200, 200)


def test_fetch_says_what_stopped_it(fake_hub):
    fake_hub(repos={"a/one": {"x": 5}}, fail="a/one")

    problem = speech.fetch(sys.executable, [Repo("a/one")], lambda done, total: None)

    assert problem == "a/one could not be downloaded: OSError: disk on fire"


def test_fetch_with_a_python_that_is_not_there_says_so():
    problem = speech.fetch("/no/such/python", [Repo("a/one")], lambda done, total: None)

    assert problem is not None and "could not run" in problem


def test_fetch_cancelled_stops_the_download(fake_hub):
    """The app's Cancel: the child is stopped, never left downloading behind it."""
    fake_hub(repos={"a/one": {"x": 5}}, hang=True)
    children = []

    def progress(done, total):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        speech.fetch(sys.executable, [Repo("a/one")], progress, started=children.append)

    [child] = children
    assert child.poll() is not None


def test_fetch_that_dies_without_a_word_says_how(tmp_path):
    script = tmp_path / "python"
    script.write_text("#!/bin/sh\necho 'not json'\necho '[1]'\nexit 3\n")
    script.chmod(0o755)

    problem = speech.fetch(str(script), [Repo("a/one")], lambda done, total: None)

    assert problem == "the download stopped (exit 3)"
