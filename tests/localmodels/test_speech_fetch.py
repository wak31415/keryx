"""The script speech-to-speech's own Python runs for `keryx voice download`, driven in this
process with a `huggingface_hub` that never reaches the network."""

import io
import json
import sys
import types
from pathlib import Path

import pytest

from keryx.localmodels import speech_fetch


class Info:
    def __init__(self, name: str, size: int | None, local: Path) -> None:
        self.filename, self.file_size, self.local_path = name, size, str(local)
        self.will_download = not local.exists()


class Hub:
    """`huggingface_hub`, faked: each repository's files and sizes, written when fetched."""

    def __init__(self, root: Path, repos: dict, *, fail: str | None = None,
                 dry_fail: bool = False) -> None:
        self.root, self.repos, self.fail, self.dry_fail = root, repos, fail, dry_fail
        self.asked: list[tuple] = []

    def snapshot_download(self, repo_id, *, allow_patterns=None, dry_run=False):
        self.asked.append((repo_id, allow_patterns, dry_run))
        folder = self.root / repo_id
        if dry_run:
            if self.dry_fail:
                raise ConnectionError("no network")
            return [Info(name, size, folder / name) for name, size in self.repos[repo_id].items()]
        if self.fail == repo_id:
            raise OSError("disk on fire\nand more")
        for name, size in self.repos[repo_id].items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(b"\0" * (size or 1))
        return str(folder)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    def make(repos, **kwargs) -> Hub:
        fake = Hub(tmp_path / "hub", repos, **kwargs)
        module = types.ModuleType("huggingface_hub")
        module.snapshot_download = fake.snapshot_download
        constants = types.ModuleType("huggingface_hub.constants")
        constants.HF_HUB_CACHE = str(tmp_path / "hub")
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)
        monkeypatch.setitem(sys.modules, "huggingface_hub.constants", constants)
        return fake

    return make


def run(spec, **kwargs) -> tuple[int, list[dict]]:
    out = io.StringIO()
    code = speech_fetch.main([json.dumps(spec)], out=out, **kwargs)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def test_it_plans_then_fetches_each_repository_with_its_patterns(hub):
    fake = hub({"a/one": {"config.json": 10, "model.safetensors": 90}})

    code, lines = run([{"id": "a/one", "allow": ["*.json", "*.safetensors"]}])

    assert code == 0
    assert fake.asked == [("a/one", ["*.json", "*.safetensors"], True),
                          ("a/one", ["*.json", "*.safetensors"], False)]
    assert lines[0] == {"progress": [0, 100]}
    assert lines[-2:] == [{"progress": [100, 100]}, {"done": True}]


def test_what_is_cached_already_counts_as_done(hub, tmp_path):
    hub({"a/one": {"config.json": 10, "model.safetensors": 90}})
    (tmp_path / "hub" / "a" / "one").mkdir(parents=True)
    (tmp_path / "hub" / "a" / "one" / "model.safetensors").write_bytes(b"\0")

    _code, lines = run([{"id": "a/one", "allow": None}])

    assert lines[0] == {"progress": [90, 100]}


def test_sizes_it_is_not_told_make_the_total_unknown(hub):
    hub({"a/one": {"x": None}})

    _code, lines = run([{"id": "a/one", "allow": None}])

    assert lines[0] == {"progress": [0, None]}


def test_a_download_in_progress_counts_its_partial_blob(hub, tmp_path):
    hub({"a/one": {"x": 100}})
    blobs = tmp_path / "hub" / "models--a--one" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "abc.1234.incomplete").write_bytes(b"\0" * 40)

    files = [Info("x", 100, tmp_path / "hub" / "a" / "one" / "x")]
    assert speech_fetch.done_bytes(files, [blobs], since=0.0) == 40
    assert speech_fetch.done_bytes(files, [blobs], since=2**40) == 0  # a stale one is not ours


def test_too_little_disk_fetches_nothing(hub):
    fake = hub({"a/one": {"x": 10**12}})

    code, lines = run([{"id": "a/one", "allow": None}], free=lambda path: 10**9)

    assert code == 1 and [dry for _repo, _allow, dry in fake.asked] == [True]
    assert "need " in lines[-1]["error"] and "GB free" in lines[-1]["error"]


def test_a_repository_that_cannot_be_asked_about_is_an_error(hub):
    hub({"a/one": {"x": 1}}, dry_fail=True)

    code, lines = run([{"id": "a/one", "allow": None}])

    assert code == 1
    assert lines == [{"error": "a/one could not be asked about: ConnectionError: no network"}]


def test_a_download_that_fails_says_which_and_why_in_one_line(hub):
    hub({"a/one": {"x": 1}, "b/two": {"y": 1}}, fail="b/two")

    code, lines = run([{"id": "a/one", "allow": None}, {"id": "b/two", "allow": None}])

    assert code == 1
    assert lines[-1] == {"error": "b/two could not be downloaded: OSError: disk on fire"}


def test_progress_is_polled_while_a_download_runs(hub, monkeypatch):
    monkeypatch.setattr(speech_fetch, "POLL_S", 0.01)
    fake = hub({"a/one": {"x": 10}})
    slow = fake.snapshot_download

    def snapshot_download(repo_id, **kwargs):
        if not kwargs.get("dry_run"):
            import time
            time.sleep(0.1)
        return slow(repo_id, **kwargs)

    sys.modules["huggingface_hub"].snapshot_download = snapshot_download

    _code, lines = run([{"id": "a/one", "allow": None}])

    assert len([line for line in lines if "progress" in line]) > 2


def test_its_cache_folder_is_hugging_faces_own_name():
    assert speech_fetch.repo_folder("mlx-community/Kokoro-82M-bf16") == \
        "models--mlx-community--Kokoro-82M-bf16"
