"""The documentation has to keep up with the code, and only a test makes it.

Two things drifted far enough to be filed as bugs before this existed: `.env.example` was
missing ten settings `Settings` reads, and the README listed thirteen of the voice model's
tools out of eighteen — including neither `recall` nor `mark_reported`, both of which are
load-bearing for how continuity works. Both were the sort of drift nobody notices, because
nothing that runs looks at either file.
"""

import re
from pathlib import Path

from jarvis.config import Settings, env_var_name

#: The repository root, so the test does not depend on the working directory pytest ran in.
ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / ".env.example"
#: Every module registrations can live in. `builtin.py` is the aggregator and the five
#: `builtin_*.py` are the domains it calls, so a new domain module is picked up by the glob
#: rather than by somebody remembering to add it here.
BUILTIN_DIR = ROOT / "src" / "jarvis" / "tools"

#: The README's tool table, fenced so the test has an unambiguous region to read.
TOOL_TABLE = re.compile(r"<!-- tools:start -->(.*?)<!-- tools:end -->", re.S)
#: An assignment line in `.env.example`, commented-out ones included.
ENV_ASSIGNMENT = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", re.M)

#: Names `.env.example` may carry that `Settings` does not read. Only the scripts read
#: these, and the file is where somebody would look for them.
SCRIPT_ONLY_ENV_NAMES = frozenset({"CLOUDFLARE_TUNNEL"})


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


def settings_env_names() -> set[str]:
    """The name each setting is *meant* to be set under: its alias, else the field upcased.

    Narrower on purpose than `conftest._settings_env_var_names`, which also includes the
    bare field name because `populate_by_name=True` makes `PIN=` work as well as
    `JARVIS_PIN=`. That matters for stripping the ambient environment; it would be wrong
    here, where the question is which name the file should document.
    """
    return {env_var_name(field) for field in Settings.model_fields}


def documented_env_names() -> set[str]:
    return set(ENV_ASSIGNMENT.findall(ENV_EXAMPLE.read_text()))


def test_env_example_lists_every_setting_that_settings_reads():
    """It is the only place all of them are written down, and nothing that runs reads it."""
    missing = settings_env_names() - documented_env_names()

    assert missing == set(), f"absent from .env.example: {sorted(missing)}"


def test_env_example_lists_nothing_that_is_not_read():
    """A setting removed from `Settings` and left here is an instruction to do nothing."""
    stale = documented_env_names() - settings_env_names() - SCRIPT_ONLY_ENV_NAMES

    assert stale == set(), f".env.example names {sorted(stale)}, which nothing reads"


def test_the_readme_documents_exactly_the_tools_that_are_registered():
    documented, registered = documented_tool_names(), registered_tool_names()

    assert documented - registered == set(), "the README lists a tool that no longer exists"
    assert registered - documented == set(), "a registered tool is missing from the README"
