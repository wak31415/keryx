"""A pytest plugin: a directory collected twice keeps its conftest's fixtures both times.

pytest 9.1 defers a directory's conftest fixtures until that directory is collected, then
registers them on that one collector node and matches by node. A command line that leaves a
directory and comes back to it (`tests/plugins/a.py tests/b.py tests/plugins/c.py`) makes a
second node for the same directory, which is given nothing: its tests silently get the
root's fixture of the same name, or none. This registers the conftest's fixtures on every
later node for a directory already seen. `tests/conftest.py` turns it on for the suite;
`test_repeated_directories.py` is the regression test.
"""

from pathlib import Path

import pytest

NAME = "repeated-directories"


class RepeatedDirectories:
    def __init__(self) -> None:
        self.seen: set[Path] = set()

    @pytest.hookimpl(wrapper=True)
    def pytest_make_collect_report(self, collector: pytest.Collector):
        report = yield
        if isinstance(collector, pytest.Directory):
            if collector.path in self.seen:
                conftest = collector.config.pluginmanager.get_plugin(
                    str(collector.path / "conftest.py")
                )
                if conftest is not None:
                    collector.session._fixturemanager.parsefactories(
                        holder=conftest, node=collector
                    )
            self.seen.add(collector.path)
        return report


def register(config: pytest.Config) -> None:
    if not config.pluginmanager.has_plugin(NAME):
        config.pluginmanager.register(RepeatedDirectories(), NAME)


def pytest_configure(config: pytest.Config) -> None:
    register(config)
