"""What this machine can hold: its GPU, its memory, and the disk the models go on."""

import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

GIB = 1024**3
#: macOS keeps about a quarter of unified memory for itself; the GPU may wire the rest.
UNIFIED_SHARE = 0.75

Accelerator = Literal["cuda", "metal", "cpu"]


@dataclass(frozen=True)
class Hardware:
    """The accelerator and the memory a model would have to fit in."""

    accelerator: Accelerator
    #: The GPU's name, or None without one Keryx can use.
    gpu: str | None
    #: Dedicated video memory (the largest card's), in GB; 0 without a discrete GPU.
    vram_gb: float
    ram_gb: float

    @property
    def model_memory_gb(self) -> float:
        """What a model may take: a discrete GPU's memory, most of a Mac's unified memory,
        or — on the CPU, where it will run but slowly — most of the RAM."""
        if self.accelerator == "cuda":
            return self.vram_gb
        return round(self.ram_gb * UNIFIED_SHARE, 1)

    def describe(self) -> str:
        if self.accelerator == "cuda":
            return f"{self.gpu}, {self.vram_gb:.0f} GB of video memory; {self.ram_gb:.0f} GB RAM"
        if self.accelerator == "metal":
            return f"{self.gpu}, {self.ram_gb:.0f} GB of unified memory"
        return f"no GPU Keryx can use, {self.ram_gb:.0f} GB RAM (models run, slowly)"


def detect(run: Callable[..., Any] = subprocess.run) -> Hardware:
    """This machine's accelerator and memory: NVIDIA by `nvidia-smi`, Apple silicon by its
    architecture, and the CPU otherwise."""
    ram = _ram_gb(run)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return Hardware("metal", "Apple silicon", 0.0, ram)
    nvidia = _nvidia(run)
    if nvidia is not None:
        name, vram = nvidia
        return Hardware("cuda", name, vram, ram)
    return Hardware("cpu", None, 0.0, ram)


def _nvidia(run: Callable[..., Any]) -> tuple[str, float] | None:
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        result = run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    cards = []
    for line in (result.stdout or "").splitlines() if result.returncode == 0 else []:
        name, _, mib = line.rpartition(",")
        try:
            cards.append((name.strip(), float(mib) / 1024))
        except ValueError:
            continue
    if not cards:
        return None
    name, vram = max(cards, key=lambda card: card[1])
    return name, round(vram, 1)


def _ram_gb(run: Callable[..., Any]) -> float:
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / GIB, 1)
    except (ValueError, OSError, AttributeError):
        pass
    try:  # macOS has no SC_PHYS_PAGES in every Python build
        result = run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
        return round(int(result.stdout.strip()) / GIB, 1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def free_bytes(path: Path) -> int:
    """Free space on the filesystem `path` is, or would be, on."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free
