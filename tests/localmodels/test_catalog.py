import pytest

from keryx.localmodels.catalog import (
    MODELS,
    VOICE_SERVER_GB,
    VOICES,
    budget,
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
    ("machine", "voice_here", "expected"),
    [
        (RTX_5090, False, "qwen3-coder-30b-a3b"),  # the tested one wins when it fits
        (RTX_5090, True, "qwen3-coder-30b-a3b"),
        (MAC_128, False, "qwen3-coder-30b-a3b"),
        (RTX_4060, False, "qwen3.5-4b"),  # otherwise the largest that fits
        (Hardware("cuda", "RTX 4080", 16, 64), False, "gpt-oss-20b"),
        (TINY, False, None),
    ],
)
def test_recommend(machine, voice_here, expected):
    best = recommend(machine, voice_here=voice_here)
    assert (best.key if best else None) == expected


def test_the_voice_servers_share_is_kept_free():
    tested = by_key("qwen3-coder-30b-a3b")
    tight = Hardware("cuda", "RTX 4090", 26.0, 64)
    assert fits(tested, tight) and not fits(tested, tight, voice_here=True)


# --- the budget: the agent's model, the voice's own, and the speech models ----------------

#: Macs by their memory; a Mac gives a model 0.75 of it.
MACS = [Hardware("metal", "Apple silicon", 0, gb)
        for gb in (8, 16, 24, 32, 36, 48, 64, 96, 128)]
CODER, GPT_20B, GPT_120B = by_key("qwen3-coder-30b-a3b"), by_key("gpt-oss-20b"), by_key(
    "gpt-oss-120b")
SMALL, NINE = by_key("qwen3.5-4b"), by_key("qwen3.5-9b")


def smallest_mac(agent, voice_llm):
    """The smallest Mac the design's table says holds `agent` and `voice_llm` (None: shared)
    with the voice here."""
    gb = voice_llm.memory_gb if voice_llm is not None else 0.0
    return next(mac.ram_gb for mac in MACS
                if fits(agent, mac, voice_here=True, voice_llm_gb=gb))


@pytest.mark.parametrize(("agent", "voice_llm", "total", "mac", "on_a_5090"), [
    (CODER, None, 28, 48, True),       # shared: the agent's model and the speech models
    (CODER, SMALL, 34, 48, False),
    (CODER, NINE, 37, 64, False),
    (CODER, GPT_20B, 44, 64, False),
    (CODER, CODER, 53, 96, False),     # the same model again, on a server of its own
    (GPT_20B, SMALL, 25, 36, True),    # the design's table said 48; a 36 GB Mac gives 27
    (GPT_120B, SMALL, 79, 128, False),
])
def test_the_budget_is_the_designs_memory_table(agent, voice_llm, total, mac, on_a_5090):
    gb = voice_llm.memory_gb if voice_llm is not None else 0.0

    assert budget(agent.memory_gb, voice_llm_gb=gb, voice_here=True) == total
    assert smallest_mac(agent, voice_llm) == mac
    assert fits(agent, RTX_5090, voice_here=True, voice_llm_gb=gb) is on_a_5090


def test_a_voice_elsewhere_costs_nothing_here_and_its_llm_only_when_it_is_here():
    # A remote or cloud voice LLM: the agent and the speech models, as today.
    assert budget(25, voice_here=True) == 25 + VOICE_SERVER_GB
    # The voice not here: no speech models, and a voice LLM only while it runs here.
    assert budget(25) == 25
    assert budget(25, voice_llm_gb=6) == 31
    assert budget(0, voice_llm_gb=9, voice_here=True) == 12


def test_recommend_reserves_the_voices_own_llm_too():
    mac_48 = MACS[5]  # 36 GB for models
    best = recommend(mac_48, voice_here=True, voice_llm_gb=9)
    # The tested 25 GB model and a 9 GB voice LLM need 37: the largest that fits is next.
    assert best.key == "qwen3.8-27b"
    assert recommend(mac_48, voice_here=True, voice_llm_gb=6).key == CODER.key


def test_each_persona_has_a_local_voice_and_any_other_name_the_default():
    names = [name for name, _ in VOICES]
    assert persona_voice("Lyra") == "af_heart" and persona_voice("Jarvis") == "bm_george"
    assert persona_voice("Ada") == "af_heart"
    assert {"af_heart", "bm_george"} <= set(names)
    assert by_key("nope") is None


# --- the voice LLM a new install runs (D2) -------------------------------------------------

MAC = {gb: Hardware("metal", "Apple silicon", 0, gb) for gb in (8, 16, 36, 48, 64)}


def test_the_voice_llm_models_are_small_catalog_models_most_worth_having_first():
    from keryx.localmodels.catalog import VOICE_LLM_MODELS

    entries = [by_key(key) for key in VOICE_LLM_MODELS]
    assert all(entry is not None for entry in entries)
    assert [entry.memory_gb for entry in entries] == [9, 6]


@pytest.mark.parametrize(("machine", "agent_gb", "picked"), [
    # qwen3-coder-30b-a3b (25 GB) beside each, with the speech models' 3 GB:
    (MAC[64], 25, "qwen3.5-9b"),  # 25 + 9 + 3 = 37 <= 48
    (MAC[48], 25, "qwen3.5-4b"),  # 25 + 9 + 3 = 37 > 36; 25 + 6 + 3 = 34 <= 36
    (MAC[36], 25, None),          # 25 + 6 + 3 = 34 > 27: shared
    (RTX_5090, 25, None),         # 34 > 31.8: shared
    # gpt-oss-20b (16 GB): 16 + 6 + 3 = 25 <= 27; 16 + 9 + 3 = 28 > 27.
    (MAC[36], 16, "qwen3.5-4b"),
    # No agent's model on this machine: the voice LLM and the speech models alone.
    (MAC[16], 0, "qwen3.5-9b"),   # 0 + 9 + 3 = 12 <= 12
    (MAC[8], 0, None),            # 0 + 6 + 3 = 9 > 6: shared
    # An agent's model here whose size Keryx cannot tell: never a second one beside it.
    (MAC[64], None, None),
])
def test_a_new_installs_voice_llm_is_its_own_small_model_only_when_it_fits(
    machine, agent_gb, picked
):
    from keryx.localmodels.catalog import voice_llm_pick

    entry = voice_llm_pick(machine, agent_gb=agent_gb)

    assert (entry.key if entry is not None else None) == picked
