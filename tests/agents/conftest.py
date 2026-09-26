"""Each backend's own tests need its SDK — its extra — and are skipped without it.

`uv sync` installs every extra, and CI does too, so they all run there; a machine synced
with one agent (`uv sync --no-group agents --extra codex`) runs the rest of the suite.
"""

from importlib.util import find_spec

collect_ignore = [
    test
    for test, module in (("test_claude.py", "claude_agent_sdk"), ("test_codex.py", "openai_codex"))
    if find_spec(module) is None
]
