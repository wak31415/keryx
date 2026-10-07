"""The voice server's speech models fetched into the Hugging Face cache: the script
`keryx voice download` runs with speech-to-speech's own Python (`speech.fetch`), so the
`huggingface_hub` that fetches them is the one the server will read them back with.

Keryx never imports this to run it: it needs nothing but the standard library and that
`huggingface_hub`, and it is run by its path. Its one argument is JSON — a list of
`{"id": repository, "allow": patterns or null}` — and it writes JSON lines to stdout:
`{"progress": [done, total]}` (bytes; `total` null when Hugging Face does not say every
size), then `{"done": true}` and exit 0, or `{"error": sentence}` and exit 1.

It asks Hugging Face first what it would fetch (`dry_run`), so the total is known and a disk
too small is said before anything is written; then it fetches each repository in turn while
a thread tells what is on disk: the files already in place, and the partial blobs Hugging
Face is writing (`blobs/*.incomplete`) since it began.
"""

import json
import shutil
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

#: How often the bytes on disk are told while a download runs.
POLL_S = 0.5
#: Free space a download must leave behind, beyond what it fetches.
MARGIN_BYTES = 1024**3


def repo_folder(repo_id: str) -> str:
    """The repository's folder in the cache, as Hugging Face names it."""
    return "models--" + repo_id.replace("/", "--")


def done_bytes(files: Sequence[Any], blobs: Sequence[Path], *, since: float) -> int:
    """What is on disk of `files`: the ones in place, and the partial blobs written since."""
    done = sum(f.file_size or 0 for f in files if Path(f.local_path).exists())
    for folder in blobs:
        for part in folder.glob("*.incomplete") if folder.is_dir() else []:
            try:
                stat = part.stat()
            except OSError:
                continue  # finished, and moved into place, while it was being counted
            if stat.st_mtime >= since:
                done += stat.st_size
    return done


def _free(path: Path) -> int:
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def _said(error: BaseException) -> str:
    first = (str(error).strip().splitlines() or [""])[0][:200]
    return f"{type(error).__name__}: {first}" if first else type(error).__name__


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    free: Callable[[Path], int] = _free,
) -> int:
    from huggingface_hub import snapshot_download
    from huggingface_hub.constants import HF_HUB_CACHE

    repos = json.loads((sys.argv[1:] if argv is None else argv)[0])
    stream = out or sys.stdout

    def say(**event: Any) -> None:
        stream.write(json.dumps(event) + "\n")
        stream.flush()

    plans = []
    for repo in repos:
        try:
            plans.append(snapshot_download(repo["id"], allow_patterns=repo["allow"],
                                           dry_run=True))
        except Exception as error:  # the network, a repository gone: said, in a line
            say(error=f"{repo['id']} could not be asked about: {_said(error)}")
            return 1
    files = [info for plan in plans for info in plan]
    known = all(info.file_size is not None for info in files)
    total = sum(info.file_size or 0 for info in files) if known else None
    remaining = sum(info.file_size or 0 for info in files if info.will_download)
    hub = Path(HF_HUB_CACHE)
    if remaining and free(hub) < remaining + MARGIN_BYTES:
        say(error=f"the speech models need {(remaining + MARGIN_BYTES) / 1e9:.1f} GB free "
                  f"in {hub}, and there is {free(hub) / 1e9:.1f} GB")
        return 1
    blobs = [hub / repo_folder(repo["id"]) / "blobs" for repo in repos]
    began = time.time()

    def tell() -> None:
        say(progress=[done_bytes(files, blobs, since=began), total])

    tell()
    stop = threading.Event()

    def poll() -> None:
        while not stop.wait(POLL_S):
            tell()

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    failed = None
    try:
        for repo in repos:
            try:
                snapshot_download(repo["id"], allow_patterns=repo["allow"])
            except Exception as error:
                failed = f"{repo['id']} could not be downloaded: {_said(error)}"
                break
    finally:
        stop.set()
        poller.join()
    if failed is not None:
        say(error=failed)
        return 1
    tell()
    say(done=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - run by speech-to-speech's own Python
    sys.exit(main())
