import subprocess
from types import SimpleNamespace

import pytest

from keryx.localmodels import hardware
from keryx.localmodels.hardware import Hardware, detect, free_bytes


def fake_run(stdout="", code=0, raises=None):
    def run(argv, **kwargs):
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=code, stdout=stdout)

    return run


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hardware, "_ram_gb", lambda run: 62.0)


def test_an_nvidia_card_is_its_name_and_its_memory(linux, monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    found = detect(fake_run("NVIDIA GeForce RTX 3060, 12288\nNVIDIA GeForce RTX 5090, 32607\n"))

    assert found == Hardware("cuda", "NVIDIA GeForce RTX 5090", 31.8, 62.0)
    assert found.model_memory_gb == 31.8
    assert "RTX 5090, 32 GB of video memory" in found.describe()


@pytest.mark.parametrize(
    "run",
    [
        fake_run("", code=9),
        fake_run("garbage\n"),
        fake_run(raises=subprocess.TimeoutExpired("x", 1)),
    ],
)
def test_an_nvidia_smi_that_says_nothing_usable_is_the_cpu(linux, monkeypatch, run):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: "/usr/bin/nvidia-smi")

    assert detect(run).accelerator == "cpu"


def test_no_gpu_is_the_cpu_with_most_of_the_ram(linux, monkeypatch):
    monkeypatch.setattr(hardware.shutil, "which", lambda name: None)

    found = detect(fake_run())

    assert (found.accelerator, found.gpu, found.model_memory_gb) == ("cpu", None, 46.5)
    assert "slowly" in found.describe()


def test_apple_silicon_shares_its_memory(monkeypatch):
    monkeypatch.setattr(hardware.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(hardware, "_ram_gb", lambda run: 64.0)

    found = detect(fake_run())

    assert found.accelerator == "metal" and found.model_memory_gb == 48.0
    assert "64 GB of unified memory" in found.describe()


def test_ram_is_read_from_sysconf_else_sysctl(monkeypatch):
    assert hardware._ram_gb(fake_run()) > 0

    def no_sysconf(name):
        raise ValueError(name)

    monkeypatch.setattr(hardware.os, "sysconf", no_sysconf)
    assert hardware._ram_gb(fake_run(str(16 * 1024**3))) == 16.0
    assert hardware._ram_gb(fake_run(raises=OSError())) == 0.0


def test_free_bytes_asks_the_nearest_directory_that_exists(tmp_path):
    assert free_bytes(tmp_path / "not" / "yet") > 0
