"""Shared pytest fixtures."""

import pytest

from jarvis.config import Settings


@pytest.fixture
def settings(tmp_path):
    """A Settings instance with no env/.env leakage, safe for tests."""
    return Settings(
        _env_file=None,
        openai_api_key="test",
        data_dir=tmp_path / "jarvis",
    )
