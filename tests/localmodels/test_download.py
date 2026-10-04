import hashlib
import stat

import httpx
import pytest

from keryx.localmodels.catalog import ModelEntry
from keryx.localmodels.download import (
    DownloadError,
    adopt,
    already_on_disk,
    download,
    hf_cache,
    is_downloaded,
    model_path,
    remaining_bytes,
)

BODY = bytes(range(256)) * 4096  # 1 MiB
SHA = hashlib.sha256(BODY).hexdigest()
URL = "https://huggingface.co/org/model-GGUF/resolve/main/model-Q4_K_M.gguf"
ENTRY = ModelEntry(
    key="tiny", title="Tiny", repo="org/model-GGUF", file="model-Q4_K_M.gguf",
    size=len(BODY), sha256=SHA, memory_gb=1, note="",
)


def server(body=BODY, *, honour_range=True, status=None, seen=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.headers.get("range"))
        if status is not None:
            return httpx.Response(status)
        wanted = request.headers.get("range")
        if wanted and honour_range:
            start = int(wanted.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=body[start:])
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_a_download_lands_whole_and_owner_only(tmp_path):
    dest = model_path(tmp_path, ENTRY)
    progress: list[int] = []

    download(URL, dest, size=ENTRY.size, sha256=SHA, progress=progress.append, client=server())

    assert dest.read_bytes() == BODY
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.parent.stat().st_mode) == 0o700
    assert progress[0] == 0 and progress[-1] == len(BODY)
    assert is_downloaded(tmp_path, ENTRY) and remaining_bytes(tmp_path, ENTRY) == 0
    assert not dest.with_name(dest.name + ".part").exists()


def test_an_interrupted_download_picks_up_where_it_stopped(tmp_path):
    dest = model_path(tmp_path, ENTRY)
    dest.parent.mkdir(parents=True)
    dest.with_name(dest.name + ".part").write_bytes(BODY[:300_000])
    assert remaining_bytes(tmp_path, ENTRY) == len(BODY) - 300_000
    seen: list = []

    download(URL, dest, size=ENTRY.size, sha256=SHA, client=server(seen=seen))

    assert seen == ["bytes=300000-"] and dest.read_bytes() == BODY


def test_a_server_that_ignores_the_range_starts_again_rather_than_doubling(tmp_path):
    dest = model_path(tmp_path, ENTRY)
    dest.parent.mkdir(parents=True)
    dest.with_name(dest.name + ".part").write_bytes(BODY[:1000])

    download(URL, dest, size=ENTRY.size, sha256=SHA, client=server(honour_range=False))

    assert dest.read_bytes() == BODY


def test_a_file_that_is_not_the_catalogs_is_deleted_and_said(tmp_path):
    dest = model_path(tmp_path, ENTRY)

    with pytest.raises(DownloadError, match="did not arrive whole"):
        download(URL, dest, size=ENTRY.size, sha256=SHA, client=server(BODY[:-1] + b"x"))

    assert not dest.exists() and not dest.with_name(dest.name + ".part").exists()


def test_a_part_larger_than_the_file_is_thrown_away(tmp_path):
    dest = model_path(tmp_path, ENTRY)
    dest.parent.mkdir(parents=True)
    dest.with_name(dest.name + ".part").write_bytes(BODY + b"extra")

    download(URL, dest, size=ENTRY.size, sha256=SHA, client=server())

    assert dest.read_bytes() == BODY


@pytest.mark.parametrize("status", [404, 500])
def test_a_refusal_is_a_sentence(tmp_path, status):
    with pytest.raises(DownloadError, match=f"HTTP {status}"):
        download(URL, model_path(tmp_path, ENTRY), size=ENTRY.size, sha256=SHA,
                 client=server(status=status))


def test_a_broken_connection_says_it_can_be_resumed(tmp_path):
    def handle(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DownloadError, match="pick up where it left off"):
        download(URL, model_path(tmp_path, ENTRY), size=ENTRY.size, sha256=SHA,
                 client=httpx.Client(transport=httpx.MockTransport(handle)))


def test_a_file_already_there_is_not_fetched_again(tmp_path):
    dest = model_path(tmp_path, ENTRY)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(BODY)

    assert download(URL, dest, size=ENTRY.size, sha256=SHA, client=server(status=500)) == dest


def test_a_copy_in_the_hugging_face_cache_is_linked_not_copied(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    snapshot = hub / "models--org--model-GGUF" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / ENTRY.file).write_bytes(BODY)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    assert hf_cache() == hub

    found = already_on_disk(ENTRY)
    assert found == snapshot / ENTRY.file

    dest = adopt(tmp_path / "cache", ENTRY, found)
    for directory in (tmp_path / "cache", tmp_path / "cache" / "models", dest.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700, directory
    assert dest.read_bytes() == BODY and is_downloaded(tmp_path / "cache", ENTRY)
    assert dest.stat().st_ino == found.stat().st_ino  # a hard link: no second copy
    adopt(tmp_path / "cache", ENTRY, found)  # and again, harmlessly


def test_a_copy_of_the_wrong_size_is_not_taken(tmp_path):
    snapshot = tmp_path / "models--org--model-GGUF" / "snapshots" / "abc"
    snapshot.mkdir(parents=True)
    (snapshot / ENTRY.file).write_bytes(BODY[:10])
    assert already_on_disk(ENTRY, tmp_path) is None
    assert already_on_disk(ENTRY, tmp_path / "nowhere") is None


def test_the_hugging_face_cache_follows_its_own_variables(monkeypatch, tmp_path):
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert hf_cache() == tmp_path / "hf" / "hub"
    monkeypatch.delenv("HF_HOME")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert hf_cache() == tmp_path / "xdg" / "huggingface" / "hub"
