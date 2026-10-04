import pytest

from keryx.localmodels.catalog import (
    MODELS,
    VOICES,
    by_key,
    fits,
    persona_voice,
    recommend,
)
from keryx.localmodels.hardware import Hardware

RTX_5090 = Hardware("cuda", "RTX 5090", 31.8, 62)
RTX_4060 = Hardware("cuda", "RTX 4060", 8.0, 32)
MAC_128 = Hardware("metal", "Apple silicon", 0, 128)
TINY = Hardware("cpu", None, 0, 4)


def test_every_entry_is_one_file_with_a_checksum_and_a_unique_name():
    assert len({entry.key for entry in MODELS}) == len(MODELS)
    for entry in MODELS:
        assert entry.file.endswith(".gguf") and len(entry.sha256) == 64
        assert entry.memory_gb > entry.size / 1e9  # the cache needs room beside the weights
        assert entry.url == f"https://huggingface.co/{entry.repo}/resolve/main/{entry.file}"


def test_largest_first_and_exactly_one_tested():
    sizes = [entry.size for entry in MODELS]
    assert sizes == sorted(sizes, reverse=True)
    assert [entry.key for entry in MODELS if entry.tested] == ["qwen3-coder-30b-a3b"]


def test_ollama_pulls_the_same_file_from_hugging_face():
    assert by_key("qwen3-coder-30b-a3b").ollama == (
        "hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M"
    )
    assert by_key("qwen3.8-27b").ollama == "hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"
    assert by_key("gpt-oss-20b").ollama == "hf.co/ggml-org/gpt-oss-20b-GGUF:MXFP4"


@pytest.mark.parametrize(
    ("machine", "reserve", "expected"),
    [
        (RTX_5090, 0, "qwen3-coder-30b-a3b"),  # the tested one wins when it fits
        (RTX_5090, 3, "qwen3-coder-30b-a3b"),
        (MAC_128, 0, "qwen3-coder-30b-a3b"),
        (RTX_4060, 0, "qwen3.5-4b"),  # otherwise the largest that fits
        (Hardware("cuda", "RTX 4080", 16, 64), 0, "gpt-oss-20b"),
        (TINY, 0, None),
    ],
)
def test_recommend(machine, reserve, expected):
    best = recommend(machine, reserve_gb=reserve)
    assert (best.key if best else None) == expected


def test_the_voice_servers_share_is_kept_free():
    tested = by_key("qwen3-coder-30b-a3b")
    tight = Hardware("cuda", "RTX 4090", 26.0, 64)
    assert fits(tested, tight) and not fits(tested, tight, reserve_gb=3)


def test_each_persona_has_a_local_voice_and_any_other_name_the_default():
    names = [name for name, _ in VOICES]
    assert persona_voice("Lyra") == "af_heart" and persona_voice("Jarvis") == "bm_george"
    assert persona_voice("Ada") == "af_heart"
    assert {"af_heart", "bm_george"} <= set(names)
    assert by_key("nope") is None
