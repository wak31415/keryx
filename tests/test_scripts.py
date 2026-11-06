"""The two shell scripts: `install.sh` (the one-line install) and `scripts/dev.sh`.

Both run for real under a scratch HOME, with `uv`, `curl`, `cloudflared` and the installed
`keryx` replaced by stubs first on PATH that record how they were called. Nothing is
downloaded and nothing is installed.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from keryx.config.files import dump_toml

SOURCE = Path(__file__).resolve().parents[1]
SH, BASH = shutil.which("sh"), shutil.which("bash")
SYSTEM_PATH = "/usr/bin:/bin"

pytestmark = pytest.mark.skipif(
    SH is None or BASH is None or shutil.which("uv", path=SYSTEM_PATH) is not None,
    reason="needs sh and bash, and no uv of the system's own",
)


def stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def box(tmp_path):
    """A HOME, a stub directory first on PATH, and the log every stub writes to."""
    home, stubs, calls = tmp_path / "home", tmp_path / "stubs", tmp_path / "calls.log"
    home.mkdir()
    bin_dir = home / ".local" / "bin"
    record = f'echo "$(basename "$0") $*" >> "{calls}"'
    uv = f'{record}\nif [ "$1 $2" = "tool dir" ]; then echo "{bin_dir}"; fi'
    stub(stubs / "uv", uv)
    stub(bin_dir / "keryx", f'{record}\necho "keryx 9.9.9"')
    env = {"HOME": str(home), "PATH": f"{stubs}:{SYSTEM_PATH}"}
    return {"home": home, "stubs": stubs, "calls": calls, "bin": bin_dir, "env": env, "uv": uv}


def install(box, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [SH, str(SOURCE / "install.sh")], env={**box["env"], **env}, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60, check=False,
    )


def calls(box) -> list[str]:
    return box["calls"].read_text().splitlines() if box["calls"].exists() else []


# --- install.sh ---------------------------------------------------------------------------


def test_the_installer_installs_keryx_with_every_agent(box):
    result = install(box, PATH=f"{box['stubs']}:{box['bin']}:{SYSTEM_PATH}")

    assert result.returncode == 0, result.stderr
    assert "uv tool install --quiet --upgrade --python 3.12 keryx-voice[all]" in calls(box)
    assert "keryx --version" in calls(box)
    assert "Installed keryx 9.9.9." in result.stdout
    # No terminal to run the wizard in, so it says what is next instead of hanging.
    assert "Next: keryx setup" in result.stdout
    assert not any(call.startswith("uv tool update-shell") for call in calls(box))
    assert "Open a new terminal" not in result.stdout


def test_the_command_directory_is_put_on_path_when_it_is_not(box):
    result = install(box)

    assert result.returncode == 0, result.stderr
    assert "uv tool update-shell --quiet" in calls(box)
    assert f"Open a new terminal to use the keryx command (it is in {box['bin']})" in result.stdout


def test_without_uv_one_is_fetched_off_path_into_keryxs_own_directory(box):
    (box["stubs"] / "uv").unlink()
    private = box["home"] / ".local" / "share" / "keryx" / "bin"
    # What `curl … | sh` would run: uv's own installer, here one that drops the stub in place.
    stub(box["home"] / "fake-uv", box["uv"])
    installer = stub(box["home"] / "uv-installer.sh", (
        f'mkdir -p "$UV_INSTALL_DIR" && cp "{box["home"] / "fake-uv"}" "$UV_INSTALL_DIR/uv"\n'
        f'echo "modify-path=$UV_NO_MODIFY_PATH" >> "{box["calls"]}"'
    ))
    stub(box["stubs"] / "curl", f'cat "{installer}"')

    result = install(box)

    assert result.returncode == 0, result.stderr
    assert (private / "uv").is_file()
    assert "modify-path=1" in calls(box)
    assert any(call.startswith("uv tool install") for call in calls(box))
    assert (private.parent.stat().st_mode & 0o077) == 0


def test_a_version_of_the_owners_choosing_is_installed_instead(box):
    result = install(box, KERYX_PACKAGE="keryx-voice[claude]==0.2.0")

    assert result.returncode == 0, result.stderr
    wanted = "uv tool install --quiet --upgrade --python 3.12 keryx-voice[claude]==0.2.0"
    assert wanted in calls(box)


def test_another_platform_is_refused(box):
    stub(box["stubs"] / "uname", "echo FreeBSD")

    result = install(box)

    assert result.returncode == 1
    assert "Linux and macOS" in result.stderr
    assert calls(box) == []


# --- scripts/dev.sh -----------------------------------------------------------------------


def test_the_dev_loop_logs_the_tunnel_beside_the_services_logs(box, tmp_path):
    repo = tmp_path / "re po"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SOURCE / "scripts" / "dev.sh", repo / "scripts" / "dev.sh")
    stub(box["stubs"] / "cloudflared", f'echo "cloudflared $*" >> "{box["calls"]}"')
    keryx_home = tmp_path / "keryx-home"
    keryx_home.mkdir()
    (keryx_home / "config.toml").write_text(dump_toml({"PUBLIC_HOST": "keryx.example.com"}))
    env = {**box["env"], "KERYX_HOME": str(keryx_home), "KERYX_CLI": f"{sys.executable} -m keryx",
           "XDG_STATE_HOME": str(box["home"] / ".local" / "state")}

    result = subprocess.run([BASH, str(repo / "scripts" / "dev.sh"), "--demo"], env=env,
                            capture_output=True, text=True, timeout=60, check=False)

    assert result.returncode == 0, result.stderr
    logs = box["home"] / ".local" / "state" / "keryx" / "logs"
    assert (logs / "cloudflared.log").exists()
    assert "uv run keryx serve --demo" in calls(box)
    assert "webhook: https://keryx.example.com/twilio/voice" in result.stdout


def test_the_dev_loop_without_a_public_host_says_how_to_set_one(box, tmp_path):
    env = {**box["env"], "KERYX_HOME": str(tmp_path / "empty"),
           "KERYX_CLI": f"{sys.executable} -m keryx"}

    result = subprocess.run([BASH, str(SOURCE / "scripts" / "dev.sh")], env=env,
                            capture_output=True, text=True, timeout=60, check=False)

    assert result.returncode == 1
    assert "keryx config set PUBLIC_HOST" in result.stderr
