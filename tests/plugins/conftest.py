"""The plugin tests' settings: the shared ones, with a PIN and the tools directory there.

The helpers every plugin test uses are in `helpers.py` beside this.
"""

import pytest

from keryx.config import Settings


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The shared settings, with a PIN, and the tools directory there."""
    settings = settings.model_copy(update={"pin": "424242"})
    settings.ensure_dirs()
    return settings
