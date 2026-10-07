"""A test directory's conftest holds however the command line orders the files.

pytest 9.1 hands a directory's conftest fixtures to the first collector made for that
directory and no other. A command line that leaves a directory and comes back to it —
`tests/plugins/a.py tests/b.py tests/plugins/c.py` — makes a second collector for it, and
the tests under that one silently get the root's fixture of the same name instead (the
plugins' `settings`, with no directories made) or none at all (the setup wizard's
`machine`). `repeated_directories` gives the second collector the same fixtures; this runs
a pytest of its own over a small tree, in that order, to prove it.
"""

import os
import subprocess
import sys
from pathlib import Path

import repeated_directories

TREE = {
    "conftest.py": "import pytest\n\n\n@pytest.fixture\ndef where():\n    return 'root'\n",
    "sub/__init__.py": "",
    "sub/conftest.py": (
        "import pytest\n\n\n@pytest.fixture\ndef where():\n    return 'sub'\n\n\n"
        "@pytest.fixture\ndef only_here():\n    return True\n"
    ),
    "sub/test_first.py": "def test_first(where, only_here):\n    assert where == 'sub'\n",
    "test_between.py": "def test_between(where):\n    assert where == 'root'\n",
    "sub/test_again.py": "def test_again(where, only_here):\n    assert where == 'sub'\n",
}
ORDER = ["sub/test_first.py", "test_between.py", "sub/test_again.py"]


def run_tree(root: Path, *plugin: str) -> subprocess.CompletedProcess:
    for name, text in TREE.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "pytest.ini").write_text("[pytest]\n")
    env = {**os.environ, "PYTHONPATH": str(Path(repeated_directories.__file__).parent)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *plugin, *ORDER],
        cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False,
    )


def test_a_directory_visited_twice_keeps_its_conftest_fixtures(tmp_path):
    done = run_tree(tmp_path, "-p", "repeated_directories")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "3 passed" in done.stdout


def test_this_suite_runs_with_it(pytestconfig):
    assert pytestconfig.pluginmanager.has_plugin(repeated_directories.NAME)
