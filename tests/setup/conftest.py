"""A `SetupContext` on a scratch machine: a store in `JARVIS_HOME`, a data directory, and
settings read back from the store after every save — the way the real wizard reads them."""

import pytest
from pydantic import ValidationError

from jarvis.config import PLACEHOLDER_KEY, Settings
from jarvis.config.store import ConfigStore
from jarvis.setup.context import SetupContext

from .fakes import FakeWorld, ScriptedPrompter


@pytest.fixture
def machine(tmp_path, monkeypatch, every_agent_installed):
    """Where things live on the scratch machine, and a loader that reads the store."""
    monkeypatch.chdir(tmp_path)  # a legacy `.secrets/` or `.env` is looked for here
    paths = {
        "data_dir": tmp_path / "jarvis",
        "projects_root": tmp_path / "projects",
        "skills_dir": tmp_path / "skills",
    }

    def load() -> Settings:
        try:
            return Settings(_env_file=None, **paths)
        except ValidationError:
            return Settings(_env_file=None, openai_api_key=PLACEHOLDER_KEY, **paths)

    return load


@pytest.fixture
def world():
    return FakeWorld()


@pytest.fixture
def make_ctx(machine, world):
    def make(answers=(), *, review=False) -> SetupContext:
        return SetupContext(
            ui=ScriptedPrompter(answers),
            store=ConfigStore(),
            load=machine,
            probes=world.probes(),
            review=review,
        )

    return make
