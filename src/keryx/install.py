"""How this Keryx was installed, and so what to tell someone to type.

Three ways, and a message naming a command asks which one first:

- **The installer** (`install.sh`): `uv tool install keryx-voice[all]`. uv is the installer's
  business: the owner types `keryx …` and nothing else, so nothing said for this kind names
  uv. `keryx upgrade` uses it on their behalf.
- **A clone**, run with `uv run`: the contributor's path, and the only one where Keryx's own
  source is on disk to be edited and restarted into.
- **pip**: any other environment someone made by hand.
"""

import importlib.metadata
import shutil
import sys
from enum import StrEnum
from pathlib import Path

import httpx

from keryx.config.files import xdg_home
from keryx.config.settings import SOURCE_ROOT

DISTRIBUTION = "keryx-voice"
INSTALL_URL = "https://raw.githubusercontent.com/wak31415/keryx/main/install.sh"
INSTALL_COMMAND = f"curl -fsSL {INSTALL_URL} | sh"
#: What `uv tool install` writes into every environment it makes.
UV_RECEIPT = "uv-receipt.toml"
PYPI_JSON = f"https://pypi.org/pypi/{DISTRIBUTION}/json"
PYPI_TIMEOUT_S = 5.0


class Install(StrEnum):
    INSTALLER = "installer"
    CLONE = "clone"
    PIP = "pip"


def how_installed(*, source_root: Path = SOURCE_ROOT, prefix: Path | None = None) -> Install:
    """Which of the three this process is running from."""
    if (source_root / "pyproject.toml").is_file():
        return Install.CLONE
    if ((prefix or Path(sys.prefix)) / UV_RECEIPT).is_file():
        return Install.INSTALLER
    return Install.PIP


def add_agent_command(agent: str, kind: Install | None = None) -> str:
    """What installs a coding agent's SDK (its extra) into this install."""
    kind = kind or how_installed()
    if kind is Install.CLONE:
        return f"uv sync --extra {agent}"
    if kind is Install.INSTALLER:
        return INSTALL_COMMAND  # the installer puts every agent in
    return f"pip install '{DISTRIBUTION}[{agent}]'"


def release() -> str | None:
    """The installed release, from the package's metadata."""
    try:
        return importlib.metadata.version(DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return None


def newest_release(transport: httpx.BaseTransport | None = None) -> str | None:
    """The newest release on PyPI, or None when PyPI could not say."""
    try:
        with httpx.Client(transport=transport, timeout=PYPI_TIMEOUT_S) as client:
            response = client.get(PYPI_JSON)
            response.raise_for_status()
            return response.json()["info"]["version"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def private_uv() -> Path:
    """Where `install.sh` puts a uv of Keryx's own when the machine has none on PATH."""
    return xdg_home("data") / "keryx" / "bin" / "uv"


def find_uv() -> str | None:
    """The installer's own uv, else the machine's."""
    private = private_uv()
    return str(private) if private.is_file() else shutil.which("uv")


def keryx_command() -> list[str]:
    """How to run this Keryx's command, whatever PATH holds (`python -m keryx`)."""
    return [sys.executable, "-m", "keryx"]


def upgrade_steps() -> list[tuple[str, list[str]]] | None:
    """An upgrade asked for on a call: install the newest release, then prove it starts.

    Each command comes with what its failing means, in words the owner hears. None when
    this is not an installer install, which is upgraded where it was installed.
    """
    uv = find_uv()
    if how_installed() is not Install.INSTALLER or uv is None:
        return None
    return [
        ("the newest release did not install", [uv, "tool", "upgrade", "--quiet", DISTRIBUTION]),
        ("the newest release does not start", [*keryx_command(), "--version"]),
    ]
