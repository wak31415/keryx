import io
import tarfile

import pytest

from keryx.localmodels import runtimes
from keryx.localmodels.hardware import Hardware
from keryx.localmodels.runtimes import (
    LLAMA_CPP_BUILD,
    install_llama_cpp,
    llama_cpp_assets,
    llama_cpp_dir,
    llama_server,
    s2s_install_argv,
)

CUDA = Hardware("cuda", "RTX 5090", 32, 64)
CPU = Hardware("cpu", None, 0, 16)
B = LLAMA_CPP_BUILD


@pytest.mark.parametrize(
    ("machine", "system", "arch", "expected"),
    [
        (CUDA, "Linux", "x86_64", [f"llama-{B}-bin-ubuntu-cuda-13.4-x64.tar.gz",
                                    f"cudart-llama-{B}-bin-ubuntu-cuda-13.4-x64.tar.gz"]),
        (CPU, "Linux", "x86_64", [f"llama-{B}-bin-ubuntu-x64.tar.gz"]),
        (CPU, "Linux", "aarch64", [f"llama-{B}-bin-ubuntu-arm64.tar.gz"]),
        (Hardware("metal", "Apple silicon", 0, 64), "Darwin", "arm64",
         [f"llama-{B}-bin-macos-arm64.tar.gz"]),
        (CPU, "Windows", "AMD64", []),
    ],
)
def test_the_build_that_fits_the_machine(machine, system, arch, expected):
    assert llama_cpp_assets(machine, system=system, machine=arch) == expected


def tarball(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_llama_cpp_is_unpacked_into_the_cache_and_found_there(tmp_path, monkeypatch):
    monkeypatch.setattr(runtimes, "which", lambda name: None)
    fetched: list[str] = []
    archives = {
        "llama": tarball({"build/bin/llama-server": b"#!/bin/sh\n", "build/bin/libggml.so": b""}),
        "cudart": tarball({"build/bin/libcudart.so.13": b""}),
    }

    def get(url, dest):
        fetched.append(url)
        dest.write_bytes(archives["cudart" if "/cudart-" in url else "llama"])

    found = install_llama_cpp(tmp_path, CUDA, get=get, system="Linux", machine="x86_64")

    assert found == str(llama_cpp_dir(tmp_path) / "build" / "bin" / "llama-server")
    assert all(url.startswith(f"{runtimes.LLAMA_CPP_RELEASES}/{B}/") for url in fetched)
    assert (llama_cpp_dir(tmp_path) / "build" / "bin" / "libcudart.so.13").exists()
    assert not list(llama_cpp_dir(tmp_path).glob("*.tar.gz"))
    assert llama_server(tmp_path) == found
    for directory in (tmp_path, llama_cpp_dir(tmp_path).parent, llama_cpp_dir(tmp_path)):
        assert directory.stat().st_mode & 0o077 == 0, directory


def test_an_unsupported_system_or_an_odd_archive_is_a_sentence(tmp_path, monkeypatch):
    monkeypatch.setattr(runtimes, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="no release build"):
        install_llama_cpp(tmp_path, CPU, get=lambda url, dest: None, system="Windows")
    with pytest.raises(RuntimeError, match="no llama-server"):
        install_llama_cpp(tmp_path, CPU, system="Linux", machine="x86_64",
                          get=lambda url, dest: dest.write_bytes(tarball({"README": b""})))


def test_a_llama_server_on_path_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(runtimes, "which", lambda name: f"/usr/local/bin/{name}")
    assert llama_server(tmp_path) == "/usr/local/bin/llama-server"
    assert runtimes.ollama() == "/usr/local/bin/ollama"
    assert runtimes.speech_to_speech() == "/usr/local/bin/speech-to-speech"


def test_speech_to_speech_is_found_where_uv_puts_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(runtimes, "which", lambda name: None)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path))
    assert runtimes.speech_to_speech() is None
    (tmp_path / "speech-to-speech").write_text("#!/bin/sh\n")
    assert runtimes.speech_to_speech() == str(tmp_path / "speech-to-speech")
    assert llama_server(tmp_path) is None


def test_the_voice_server_installs_with_what_its_first_start_needs():
    linux = s2s_install_argv(system="Linux")
    mac = s2s_install_argv(system="Darwin")

    assert linux[:3] == ["uv", "tool", "install"]
    assert f"speech-to-speech=={runtimes.S2S_VERSION}" in linux
    assert "--managed-python" in linux and runtimes.SPACY_MODEL in linux
    assert "kokoro>=0.9.2" in linux and "kokoro>=0.9.2" not in mac
