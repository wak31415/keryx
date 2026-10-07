import io
import tarfile

import httpx
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


@pytest.mark.parametrize(("length", "total"), [("6", 6), (None, None)])
def test_a_fetch_tells_its_progress_with_the_size_when_it_is_given(
    tmp_path, monkeypatch, length, total
):
    def serve(self, request):
        headers = {"content-length": length} if length else {}
        return httpx.Response(200, headers=headers, content=iter([b"abc", b"def"]),
                              request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve)
    told: list[tuple] = []

    runtimes.fetch("https://example.invalid/a.tar.gz", tmp_path / "a", progress=lambda *a:
                   told.append(a))

    assert (tmp_path / "a").read_bytes() == b"abcdef"
    assert told[-1] == (6, total)


def tool(tmp_path):
    """speech-to-speech as `uv tool install` leaves it: an environment with its command and
    its Python in `bin/`, and the command linked from the tool bin directory."""
    env = tmp_path / "uv" / "tools" / "speech-to-speech" / "bin"
    env.mkdir(parents=True)
    (env / "python").write_text("")
    command = env / "speech-to-speech"
    command.write_text(f"#!{env / 'python'}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "speech-to-speech").symlink_to(command)
    return env, bin_dir / "speech-to-speech"


def test_speech_to_speechs_python_is_the_one_in_its_own_environment(tmp_path, monkeypatch):
    env, linked = tool(tmp_path)
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: str(linked))

    assert runtimes.speech_to_speech_python() == str(env / "python")
    assert runtimes.speech_to_speech_python(str(linked)) == str(env / "python")


def test_speech_to_speechs_python_is_read_from_its_first_line_when_it_is_a_copy(tmp_path):
    env, _linked = tool(tmp_path)
    copy = tmp_path / "copied" / "speech-to-speech"
    copy.parent.mkdir()
    copy.write_text(f"#!{env / 'python'} -E\nimport sys\n")

    assert runtimes.speech_to_speech_python(str(copy)) == str(env / "python")


def test_no_speech_to_speech_has_no_python(tmp_path, monkeypatch):
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: None)
    assert runtimes.speech_to_speech_python() is None

    odd = tmp_path / "speech-to-speech"
    odd.write_bytes(b"\x7fELF binary")
    assert runtimes.speech_to_speech_python(str(odd)) is None
    assert runtimes.speech_to_speech_python(str(tmp_path / "gone")) is None
    shebang = tmp_path / "other"
    shebang.write_text("#!/no/such/python\n")
    assert runtimes.speech_to_speech_python(str(shebang)) is None


def test_a_loop_of_links_has_no_python(tmp_path):
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")

    assert runtimes.speech_to_speech_python(str(tmp_path / "a")) is None
