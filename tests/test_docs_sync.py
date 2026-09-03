"""The documentation has to keep up with the code, and only a test makes it.

Two things drifted far enough to be filed as bugs before this existed: `.env.example` was
missing ten settings `Settings` reads, and the README listed thirteen of the voice model's
tools out of eighteen — including neither `recall` nor `mark_reported`, both of which are
load-bearing for how continuity works. Both were the sort of drift nobody notices, because
nothing that runs looks at either file.
"""

import re
from pathlib import Path

#: The repository root, so the test does not depend on the working directory pytest ran in.
ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
#: Every module registrations can live in. `builtin.py` is the aggregator and the five
#: `builtin_*.py` are the domains it calls, so a new domain module is picked up by the glob
#: rather than by somebody remembering to add it here.
BUILTIN_DIR = ROOT / "src" / "jarvis" / "tools"

#: The README's tool table, fenced so the test has an unambiguous region to read.
TOOL_TABLE = re.compile(r"<!-- tools:start -->(.*?)<!-- tools:end -->", re.S)


def registered_tool_names() -> set[str]:
    """Every name passed to `registry.register(...)` under `tools/builtin*.py`.

    Read out of the source rather than by registering for real: registration needs a
    `TaskManager`, a broker and half the application wired up, and this test is about what
    the files say, not about what a particular wiring produces.
    """
    names: set[str] = set()
    for path in sorted(BUILTIN_DIR.glob("builtin*.py")):
        names |= set(re.findall(r'registry\.register\(\s*\n\s*"([a-z_]+)"', path.read_text()))
    assert names, "no `registry.register(...)` calls found — has the call shape changed?"
    return names


def documented_tool_names() -> set[str]:
    match = TOOL_TABLE.search(README.read_text())
    assert match is not None, "the README's <!-- tools:start --> table is gone"
    return set(re.findall(r"^\| `([a-z_]+)` \|", match.group(1), re.M))


def test_the_readme_documents_exactly_the_tools_that_are_registered():
    documented, registered = documented_tool_names(), registered_tool_names()

    assert documented - registered == set(), "the README lists a tool that no longer exists"
    assert registered - documented == set(), "a registered tool is missing from the README"
