"""Where Keryx's directories are, and the TOML the store writes, read back by the reader
`Settings` uses."""

import tomllib
from pathlib import Path

import pytest

from keryx.config.files import (
    XDG_HOMES,
    claude_config_dir,
    claude_user_config,
    default_cache_dir,
    default_data_dir,
    default_state_dir,
    dump_toml,
    keryx_home,
    legacy_entries,
    xdg_home,
)

# --- the XDG directories ----------------------------------------------------------------


@pytest.mark.parametrize("kind", list(XDG_HOMES))
def test_each_base_directory_is_its_variable(kind, monkeypatch, tmp_path):
    monkeypatch.setenv(XDG_HOMES[kind][0], str(tmp_path / "somewhere"))

    assert xdg_home(kind) == tmp_path / "somewhere"


@pytest.mark.parametrize("kind", list(XDG_HOMES))
@pytest.mark.parametrize("value", ["", "   ", "relative/dir", "."])
def test_an_empty_or_relative_variable_is_ignored(kind, value, monkeypatch):
    """As the specification says: a relative one would follow whichever directory a
    process happened to start in."""
    monkeypatch.setenv(XDG_HOMES[kind][0], value)

    assert xdg_home(kind) == Path(XDG_HOMES[kind][1]).expanduser()


def test_without_the_variables_the_defaults_are_the_specifications(monkeypatch):
    for variable, _ in XDG_HOMES.values():
        monkeypatch.delenv(variable, raising=False)
    home = Path.home()

    assert keryx_home() != home / ".config" / "keryx"  # KERYX_HOME, set by the conftest
    monkeypatch.delenv("KERYX_HOME")
    assert keryx_home() == home / ".config" / "keryx"
    assert default_data_dir() == home / ".local" / "share" / "keryx"
    assert default_state_dir() == home / ".local" / "state" / "keryx"
    assert default_cache_dir() == home / ".cache" / "keryx"


def test_keryx_home_outranks_the_config_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("KERYX_HOME", str(tmp_path / "elsewhere"))
    assert keryx_home() == tmp_path / "elsewhere"

    monkeypatch.setenv("KERYX_HOME", "  ")
    assert keryx_home() == tmp_path / "xdg" / "keryx"


def test_the_claude_directories_are_in_home_by_default():
    assert claude_config_dir() == Path.home() / ".claude"
    assert claude_user_config() == Path.home() / ".claude.json"


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_claude_config_dir_is_ignored(value, monkeypatch):
    """Not the working directory: an empty value would put the CLI's files wherever a
    process happened to start."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", value)

    assert claude_config_dir() == Path.home() / ".claude"
    assert claude_user_config() == Path.home() / ".claude.json"


def test_claude_config_dir_moves_the_directory_and_the_user_config(monkeypatch, tmp_path):
    """The CLI keeps `.claude.json` inside the moved directory, not beside it."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))

    assert claude_config_dir() == tmp_path / "claude"
    assert claude_user_config() == tmp_path / "claude" / ".claude.json"


def test_legacy_entries_are_only_the_names_keryx_wrote(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    for name in ("tasks.db", "calls", "restart-after-task7.sh", "memory.md.real"):
        (legacy / name).touch()

    assert legacy_entries(legacy) == ["calls", "tasks.db"]
    assert legacy_entries(tmp_path / "not-there") == []


def test_legacy_entries_default_to_the_home_directorys(monkeypatch):
    legacy = Path.home() / ".jarvis"
    legacy.mkdir(parents=True)
    (legacy / "pin").touch()

    assert legacy_entries() == ["pin"]


# --- TOML -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"A": "plain", "B": 3, "C": 0.5, "D": True, "E": ["x", "y"], "F": []},
        {"QUOTES": 'say "hi"\\ and \t tab\nnewline', "UNICODE": "ünïcødé ✓"},
        {"PROJECTS": {"two words": "/a b", "plain": "/c"}, "service_writable": {"PORT": False}},
    ],
)
def test_what_is_written_reads_back_the_same(data):
    assert tomllib.loads(dump_toml(data, header="line one\nline two")) == data


def test_tables_come_after_every_plain_key():
    text = dump_toml({"T": {"k": "v"}, "A": 1})

    assert text.index("A = 1") < text.index("[T]")


def test_the_header_is_a_comment():
    assert dump_toml({}, header="hello").startswith("# hello\n")
