"""The TOML the store writes, read back by the reader `Settings` uses."""

import tomllib

import pytest

from jarvis.config.files import dump_toml


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
