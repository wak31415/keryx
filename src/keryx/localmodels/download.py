"""Downloading a model: resumable, checked, and owner-only like everything else Keryx keeps.

Models go in `CACHE_DIR/models` — what can be downloaded again — one directory per Hugging
Face repository. A download streams into `<file>.part`, picks up where an interrupted one
left off (an HTTP `Range` request), hashes as it goes, and becomes the file only when its
size and SHA-256 are the catalog's: a 20 GB file that is a byte short is a model that
loads and then says nonsense, which is worse than none.
"""

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import httpx

from keryx.config import secure_dir, secure_file
from keryx.localmodels.catalog import ModelEntry

CHUNK_BYTES = 8 * 1024 * 1024
#: Free space a download must leave behind, beyond what it still has to fetch.
DISK_MARGIN_BYTES = 1024**3
TIMEOUT = httpx.Timeout(30.0, read=120.0)

Progress = Callable[[int], None]


class DownloadError(RuntimeError):
    """A download that did not end as the file it should be, in a sentence."""


def models_dir(cache_dir: Path) -> Path:
    return cache_dir / "models"


def model_path(cache_dir: Path, entry: ModelEntry) -> Path:
    return models_dir(cache_dir) / entry.repo.replace("/", "--") / entry.file


def ensure_model_dir(cache_dir: Path, entry: ModelEntry) -> Path:
    """`model_path`, with every directory from `CACHE_DIR` down made owner-only: `secure_dir`
    tightens the one it is given, and the parents it creates would keep the umask's mode."""
    dest = model_path(cache_dir, entry)
    for directory in (cache_dir, models_dir(cache_dir), dest.parent):
        secure_dir(directory)
    return dest


def is_downloaded(cache_dir: Path, entry: ModelEntry) -> bool:
    path = model_path(cache_dir, entry)
    return path.is_file() and path.stat().st_size == entry.size


def remaining_bytes(cache_dir: Path, entry: ModelEntry) -> int:
    """What is still to fetch: all of it, less a partial download already on disk."""
    if is_downloaded(cache_dir, entry):
        return 0
    part = _part(model_path(cache_dir, entry))
    return entry.size - (part.stat().st_size if part.is_file() else 0)


def hf_cache() -> Path:
    """Where Hugging Face's own tools keep what they download."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    home = os.environ.get("HF_HOME") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or "~/.cache", "huggingface"
    )
    return Path(home).expanduser() / "hub"


def already_on_disk(entry: ModelEntry, hub: Path | None = None) -> Path | None:
    """The same file, downloaded before by a Hugging Face tool (LM Studio, llama.cpp's
    `-hf`, `huggingface-cli`), when its size is right: no need to fetch it twice."""
    org, name = entry.repo.split("/", 1)
    snapshots = (hub or hf_cache()) / f"models--{org}--{name}" / "snapshots"
    for found in sorted(snapshots.glob(f"*/{entry.file}")) if snapshots.is_dir() else []:
        if found.is_file() and found.stat().st_size == entry.size:
            return found
    return None


def adopt(cache_dir: Path, entry: ModelEntry, found: Path) -> Path:
    """Put `found` where Keryx looks for `entry`, as a link: a hard one on the same disk,
    else a symbolic one. Nothing is copied."""
    dest = ensure_model_dir(cache_dir, entry)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    target = found.resolve()
    try:
        os.link(target, dest)
    except OSError:
        dest.symlink_to(target)
    return dest


def _part(path: Path) -> Path:
    return path.with_name(path.name + ".part")


def download(
    url: str,
    dest: Path,
    *,
    size: int,
    sha256: str,
    progress: Progress = lambda _done: None,
    client: httpx.Client | None = None,
) -> Path:
    """Fetch `url` to `dest`, resuming a `.part` left before; `DownloadError` on a mismatch.

    `progress` is told the bytes on disk so far, the resumed ones first.
    """
    if dest.is_file() and dest.stat().st_size == size:
        return dest
    secure_dir(dest.parent)
    part = _part(dest)
    digest = hashlib.sha256()
    done = 0
    if part.is_file():
        with part.open("rb") as existing:
            while chunk := existing.read(CHUNK_BYTES):
                digest.update(chunk)
                done += len(chunk)
        if done > size:
            part.unlink()
            digest, done = hashlib.sha256(), 0
    progress(done)
    own = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=TIMEOUT)
    try:
        if done < size:
            done, digest = _fetch(http, url, part, done, digest, progress)
    except httpx.HTTPError as exc:
        raise DownloadError(f"the download stopped ({type(exc).__name__}); run it again to "
                            "pick up where it left off") from None
    finally:
        if own:
            http.close()
    if done != size or digest.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise DownloadError(f"{dest.name} did not arrive whole (its size or checksum is "
                            "wrong); it was deleted, so try again")
    secure_file(part)
    os.replace(part, dest)
    return dest


def _fetch(
    http: httpx.Client, url: str, part: Path, done: int, digest: "hashlib._Hash",
    progress: Progress,
) -> tuple[int, "hashlib._Hash"]:
    headers = {"Range": f"bytes={done}-"} if done else {}
    with http.stream("GET", url, headers=headers) as response:
        if done and response.status_code == 200:
            # The server ignored the range: start again rather than append a second copy.
            part.unlink(missing_ok=True)
            digest, done = hashlib.sha256(), 0
        elif response.status_code not in (200, 206):
            raise DownloadError(f"Hugging Face answered HTTP {response.status_code} for {url}")
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as out:
            for chunk in response.iter_bytes(CHUNK_BYTES):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done)
    return done, digest
