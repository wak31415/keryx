"""Fixtures shared by the approval-bridge tests.

The bridge listens on a Unix socket, and a Unix socket path has a hard length limit that
pytest's own temp directories can exceed. That is the whole reason this file exists.
"""

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def short_tmp_path():
    """A temp directory short enough to hold a unix socket path.

    pytest's `tmp_path` is not, on macOS: it lives under `/private/var/folders/…` and,
    with the test's name and `keryx/approvals.sock` on the end, comfortably exceeds the
    ~104-byte `sun_path` limit — so `ApprovalBroker.start()` returns False and every test
    fails on the fixture rather than on anything it was checking. Nothing about Keryx
    needs a long path: `~/.local/state/keryx/approvals.sock` is forty-odd characters. Only
    the fixture did, and this is where that gets fixed once for every file that binds one.
    """
    root = Path("/tmp") if os.access("/tmp", os.W_OK) else None
    with tempfile.TemporaryDirectory(prefix="jb", dir=root) as name:
        yield Path(name)
