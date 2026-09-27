"""The documentation has to keep up with the code, and only a test makes it.

Two things drifted far enough to be filed as bugs before this existed: `.env.example` was
missing ten settings `Settings` reads, and the README listed thirteen of the voice model's
tools out of eighteen — including neither `recall` nor `mark_reported`, both of which are
load-bearing for how continuity works. Both were the sort of drift nobody notices, because
nothing that runs looks at either file.
"""

import re
from pathlib import Path

from jarvis.agents.registry import BACKENDS
from jarvis.config import Settings, env_var_name

#: The repository root, so the test does not depend on the working directory pytest ran in.
ROOT = Path(__file__).resolve().parents[1]
TOOLS_DOC = ROOT / "docs" / "tools.md"
ENV_EXAMPLE = ROOT / ".env.example"
#: Every module registrations can live in. `builtin.py` is the aggregator and the five
#: `builtin_*.py` are the domains it calls, so a new domain module is picked up by the glob
#: rather than by somebody remembering to add it here.
BUILTIN_DIR = ROOT / "src" / "jarvis" / "tools"

#: The tool table in `docs/tools.md`, fenced so the test has an unambiguous region to read.
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
    match = TOOL_TABLE.search(TOOLS_DOC.read_text())
    assert match is not None, "the <!-- tools:start --> table in docs/tools.md is gone"
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


def test_docs_tools_documents_exactly_the_tools_that_are_registered():
    documented, registered = documented_tool_names(), registered_tool_names()

    assert documented - registered == set(), "docs/tools.md lists a tool that no longer exists"
    assert registered - documented == set(), "a registered tool is missing from docs/tools.md"


#: Snake-case words in the prompt's "Your tools" section that are not tool names: three
#: tool *arguments* the model has to pass by name, a Gmail search operator it is shown as
#: an example, and the status a gated tool returns.
NOT_TOOLS = frozenset({"wait_seconds", "task_id", "gmail_query", "newer_than", "pin_required"})


def prompt_tool_names() -> set[str]:
    """Every snake-case name the voice prompt uses from "Your tools" onwards.

    Read from that heading down because the sections above it are prose about the call,
    where a word like `recall` is English rather than a tool. The `voice_tool_*.md`
    paragraphs count too: they describe the tools only some machines offer, and are spliced
    into that section, in place of a `{placeholder}`, when the tool is registered.
    """
    prompts = ROOT / "src" / "jarvis" / "prompts"
    text = (prompts / "voice_system.md").read_text()
    body = re.sub(r"\{[a-z_]+\}", "", text[text.index("## Your tools") :])
    body += "".join(path.read_text() for path in sorted(prompts.glob("voice_tool_*.md")))
    return set(re.findall(r"\b([a-z]+_[a-z_]+)\b", body)) - NOT_TOOLS


AGENTS_DOC = ROOT / "docs" / "agents.md"
#: The parity table in `docs/agents.md`, fenced the same way as the tool table.
AGENT_TABLE = re.compile(r"<!-- agents:start -->(.*?)<!-- agents:end -->", re.S)


def test_docs_agents_has_a_column_for_every_agent_jarvis_knows():
    """A third agent added to the registry without a column is a parity nobody wrote down."""
    match = AGENT_TABLE.search(AGENTS_DOC.read_text())
    assert match is not None, "the <!-- agents:start --> table in docs/agents.md is gone"
    header = match.group(1).strip().splitlines()[0]
    columns = {cell.strip().lower() for cell in header.strip("|").split("|")}

    assert set(BACKENDS) <= columns, f"docs/agents.md has no column for {set(BACKENDS) - columns}"


def test_every_tool_the_voice_prompt_names_is_one_that_exists():
    """A renamed or dropped tool leaves prose telling the model to call something gone.

    Nothing at runtime notices: the model calls a name the registry does not have and has
    to apologize out loud, mid-call, for a rename nobody finished.
    """
    unknown = prompt_tool_names() - registered_tool_names()

    assert not unknown, f"the prompt names tools that do not exist: {sorted(unknown)}"


def test_the_voice_prompt_still_describes_the_tools_the_model_is_given():
    """The other direction: a tool nobody told the model about is a tool it will not use.

    Only the two-word names, because `prompt_tool_names` has to read snake_case to tell a
    tool from ordinary prose — `recall` is a real tool and also a real English word.
    """
    two_word = {name for name in registered_tool_names() if "_" in name}
    undocumented = two_word - prompt_tool_names()

    assert not undocumented, f"the prompt does not mention: {sorted(undocumented)}"


def test_the_prompt_tells_the_model_not_to_announce_the_instant_tools():
    """The list of tools too fast to be worth announcing has to stay a list of real ones."""
    text = (ROOT / "src" / "jarvis" / "prompts" / "voice_system.md").read_text()
    sentence = text[text.index("all answer in\n  milliseconds") - 400 :][:500]
    sentence = re.sub(r"\{[a-z_]+\}", "", sentence)

    instant = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", sentence)) - NOT_TOOLS
    assert instant, "the instant-tool list is gone from the prompt"
    assert instant <= registered_tool_names(), sorted(instant - registered_tool_names())
