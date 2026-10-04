"""The models `keryx setup` offers to download, and which of them fits this machine.

A short list on purpose, LM Studio-style: open models that call tools well enough to run
inside Claude Code or Codex, as one GGUF each from Hugging Face, at the quantization that
is the usual trade for a single GPU. Every file, size and SHA-256 below was read from the
Hugging Face API on 2026-10-04; a download is checked against them.

`memory_gb` is what the model needs with its context's worth of cache, not just the file:
the agents' system prompts alone are about 18,000 tokens, so a model is given 64k tokens of
context where it can afford them, and 32k where it cannot. `tested` is the one model that
ran every path in Keryx end to end — a task through each harness and a phone-style call.
"""

from dataclasses import dataclass

from keryx.localmodels.hardware import Hardware

#: Video memory the voice server's own models take (speech-to-text and text-to-speech),
#: kept free when the voice runs on the same GPU.
VOICE_SERVER_GB = 3.0
#: Disk the voice server's models take on its first start (into the Hugging Face cache).
VOICE_SERVER_DOWNLOAD_GB = 3.0


@dataclass(frozen=True)
class ModelEntry:
    """One downloadable model."""

    #: Its name everywhere in Keryx — `LLM_SERVER_MODEL`, and the id the server serves it as.
    key: str
    title: str
    repo: str
    file: str
    size: int
    sha256: str
    memory_gb: float
    note: str
    context: int = 65536
    tested: bool = False

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.file}"

    @property
    def size_gb(self) -> float:
        return round(self.size / 1e9, 1)

    @property
    def ollama(self) -> str:
        """The same file as Ollama pulls it straight from Hugging Face."""
        quant = self.file.removesuffix(".gguf").rsplit("-", 1)[-1]
        if self.file.removesuffix(".gguf").endswith(f"UD-{quant}"):
            quant = f"UD-{quant}"
        return f"hf.co/{self.repo}:{quant}"


#: Largest first: the order they are offered in.
MODELS: tuple[ModelEntry, ...] = (
    ModelEntry(
        key="gpt-oss-120b",
        title="gpt-oss 120B",
        repo="ggml-org/gpt-oss-120b-GGUF",
        file="gpt-oss-120b-MXFP4.gguf",
        size=63387346208,
        sha256="582bd40f6886200101f4c4ed9f25f3fe80cc14c86e9e2b37746cd8904a0c622d",
        memory_gb=70,
        note="OpenAI's open-weight model; for a workstation card or a large Mac",
    ),
    ModelEntry(
        key="qwen3.6-35b-a3b",
        title="Qwen3.6 35B-A3B",
        repo="unsloth/Qwen3.6-35B-A3B-GGUF",
        file="Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
        size=22134528992,
        sha256="ac0e2c1189e055faa36eff361580e79c5bd6f8e76bffb4ce547f167d53e31a61",
        memory_gb=28,
        note="newest of the mixture-of-experts Qwens: fast for its size",
    ),
    ModelEntry(
        key="qwen3-coder-30b-a3b",
        title="Qwen3-Coder 30B-A3B",
        repo="unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF",
        file="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        size=18556689568,
        sha256="fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad",
        memory_gb=25,
        note="made for agentic coding; fast",
        tested=True,
    ),
    ModelEntry(
        key="qwen3.8-27b",
        title="Qwen3.8 27B",
        repo="unsloth/Qwen3.8-27B-GGUF",
        file="Qwen3.8-27B-UD-Q4_K_M.gguf",
        size=16464440224,
        sha256="322e194ff79741c7baa497c240f677f54b201b0efab44ca8e50f122b39123482",
        memory_gb=24,
        note="dense: slower than the A3B models, steadier on long tasks",
    ),
    ModelEntry(
        key="gpt-oss-20b",
        title="gpt-oss 20B",
        repo="ggml-org/gpt-oss-20b-GGUF",
        file="gpt-oss-20b-MXFP4.gguf",
        size=12109566624,
        sha256="27cd6c432c7672cb812a92f611cf3ba7bbc35928262bb1e1253ff4ee6ae35901",
        memory_gb=16,
        note="OpenAI's smaller open-weight model; good on a 16 GB card",
    ),
    ModelEntry(
        key="gemma-4-12b",
        title="Gemma 4 12B",
        repo="unsloth/gemma-4-12b-it-GGUF",
        file="gemma-4-12b-it-Q4_K_M.gguf",
        size=7121861440,
        sha256="0a270ec9fe6b34f4a0d33992b6135117b484ebc4766ab76b51d4ae8c457e4c42",
        memory_gb=11,
        note="Google's; for a 12 GB card",
        context=32768,
    ),
    ModelEntry(
        key="qwen3.5-9b",
        title="Qwen3.5 9B",
        repo="unsloth/Qwen3.5-9B-GGUF",
        file="Qwen3.5-9B-Q4_K_M.gguf",
        size=5680522464,
        sha256="03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8",
        memory_gb=9,
        note="small tasks only; for an 8–10 GB card",
        context=32768,
    ),
    ModelEntry(
        key="qwen3.5-4b",
        title="Qwen3.5 4B",
        repo="unsloth/Qwen3.5-4B-GGUF",
        file="Qwen3.5-4B-Q4_K_M.gguf",
        size=2740937888,
        sha256="00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4",
        memory_gb=6,
        note="the smallest that calls tools at all; expect mistakes",
        context=32768,
    ),
)


def by_key(key: str) -> ModelEntry | None:
    return next((entry for entry in MODELS if entry.key == key), None)


def fits(entry: ModelEntry, hardware: Hardware, *, reserve_gb: float = 0.0) -> bool:
    """Whether `entry` fits what `hardware` can give a model, less `reserve_gb`."""
    return entry.memory_gb + reserve_gb <= hardware.model_memory_gb


def recommend(hardware: Hardware, *, reserve_gb: float = 0.0) -> ModelEntry | None:
    """The tested model when it fits, else the largest that does; None when nothing does."""
    fitting = [entry for entry in MODELS if fits(entry, hardware, reserve_gb=reserve_gb)]
    tested = [entry for entry in fitting if entry.tested]
    return (tested or fitting or [None])[0]


#: The voices speech-to-speech's Kokoro speaks in, and how each sounds (American and
#: British English; Kokoro has others, which `VOICE_SERVER_VOICE` accepts by name).
VOICES: tuple[tuple[str, str], ...] = (
    ("af_heart", "warm, American, female"),
    ("af_bella", "bright, American, female"),
    ("af_nicole", "soft, American, female"),
    ("am_michael", "calm, American, male"),
    ("am_fenrir", "deep, American, male"),
    ("bf_emma", "clear, British, female"),
    ("bf_isabella", "warm, British, female"),
    ("bm_george", "measured, British, male"),
    ("bm_fable", "lively, British, male"),
    ("bm_lewis", "low, British, male"),
)
#: Each built-in persona's local voice, the nearest Kokoro has to its Realtime one.
PERSONA_VOICES = {"lyra": "af_heart", "jarvis": "bm_george"}
DEFAULT_VOICE = "af_heart"


def persona_voice(assistant: str) -> str:
    return PERSONA_VOICES.get(assistant.lower(), DEFAULT_VOICE)
