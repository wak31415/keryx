"""What serves the models: llama.cpp's `llama-server` and Hugging Face's speech-to-speech.

Keryx uses what the machine already has — a `llama-server` on PATH, or Ollama — and installs
what it lacks the way it installs itself, natively and without docker (which has no GPU on a
Mac): llama.cpp from its own release builds into `CACHE_DIR/llama.cpp` (or Homebrew on a Mac
that has it), speech-to-speech as a `uv tool`. Both are pinned, so a working setup stays
working.
"""

import os
import platform
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path

import httpx

from keryx.config import secure_dir
from keryx.localmodels.hardware import Hardware

#: The llama.cpp build installed when the machine has none (2026-10-04).
LLAMA_CPP_BUILD = "b11399"
LLAMA_CPP_RELEASES = "https://github.com/ggml-org/llama.cpp/releases/download"
#: speech-to-speech, and what its Linux install lacks: Kokoro is not a default dependency
#: there, and its phonemizer downloads a spaCy model at first use with a `pip` that a uv
#: tool does not have — so the model is installed with it (both found on 2026-10-04).
S2S_VERSION = "1.0.0"
SPACY_MODEL = (
    "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
)
#: uv's own Python: one from elsewhere (a conda base, say) can carry a certificate store
#: that cannot verify the downloads speech-to-speech makes on its first start.
S2S_PYTHON = "3.12"


def which(name: str) -> str | None:
    return shutil.which(name)


def llama_cpp_dir(cache_dir: Path) -> Path:
    return cache_dir / "llama.cpp" / LLAMA_CPP_BUILD


def llama_server(cache_dir: Path) -> str | None:
    """`llama-server`: the one on PATH, else the build Keryx installed."""
    found = which("llama-server")
    if found:
        return found
    installed = llama_cpp_dir(cache_dir)
    for candidate in sorted(installed.rglob("llama-server")) if installed.is_dir() else []:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def ollama() -> str | None:
    return which("ollama")


def speech_to_speech() -> str | None:
    """The `speech-to-speech` command: on PATH, else where `uv tool` puts commands."""
    found = which("speech-to-speech")
    if found:
        return found
    candidate = Path(os.environ.get("UV_TOOL_BIN_DIR") or "~/.local/bin").expanduser()
    candidate = candidate / "speech-to-speech"
    return str(candidate) if candidate.is_file() else None


def llama_cpp_assets(
    hardware: Hardware, *, system: str | None = None, machine: str | None = None
) -> list[str]:
    """The release archives that make a working `llama-server` here, or none: CUDA with its
    runtime beside it on an NVIDIA card, the CPU build otherwise, and macOS's own."""
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    build = LLAMA_CPP_BUILD
    if system == "Darwin":
        return [f"llama-{build}-bin-macos-{arch}.tar.gz"]
    if system != "Linux":
        return []
    if hardware.accelerator == "cuda":
        return [
            f"llama-{build}-bin-ubuntu-cuda-13.4-{arch}.tar.gz",
            f"cudart-llama-{build}-bin-ubuntu-cuda-13.4-{arch}.tar.gz",
        ]
    return [f"llama-{build}-bin-ubuntu-{arch}.tar.gz"]


def fetch(url: str, dest: Path) -> None:
    """One file, over https, to `dest`."""
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with dest.open("wb") as out:
            for chunk in response.iter_bytes(1024 * 1024):
                out.write(chunk)


def install_llama_cpp(
    cache_dir: Path,
    hardware: Hardware,
    *,
    get: Callable[[str, Path], None] = fetch,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """Download and unpack llama.cpp's release build into `CACHE_DIR/llama.cpp`; the path
    of its `llama-server`. `RuntimeError` with a sentence when there is no build for here."""
    assets = llama_cpp_assets(hardware, system=system, machine=machine)
    if not assets:
        raise RuntimeError("llama.cpp has no release build for this system; install it yourself")
    target = llama_cpp_dir(cache_dir)
    for directory in (cache_dir, target.parent, target):
        secure_dir(directory)
    for name in assets:
        archive = target / name
        get(f"{LLAMA_CPP_RELEASES}/{LLAMA_CPP_BUILD}/{name}", archive)
        with tarfile.open(archive) as tar:
            tar.extractall(target, filter="data")
        archive.unlink()
    found = next((path for path in sorted(target.rglob("llama-server")) if path.is_file()), None)
    if found is None:
        raise RuntimeError(f"the llama.cpp {LLAMA_CPP_BUILD} build has no llama-server in it")
    found.chmod(0o755)
    return str(found)


def s2s_install_argv(*, system: str | None = None) -> list[str]:
    """The `uv tool install` that makes `speech-to-speech` work on its first start."""
    argv = [
        "uv", "tool", "install", "--force", "--managed-python", "--python", S2S_PYTHON,
        f"speech-to-speech=={S2S_VERSION}", "--with", SPACY_MODEL,
    ]
    if (system or platform.system()) != "Darwin":
        argv += ["--with", "kokoro>=0.9.2", "--with", "soundfile"]
    return argv
