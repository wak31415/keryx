"""Keryx command-line interface. `apps` holds `app` and its command groups; every other
module here adds its commands to them as it is imported, and `common` is what more than one
of them needs."""

from keryx.cli.apps import app
from keryx.cli.common import renamed

# In the order `keryx --help` lists the commands: the root commands are listed as their
# modules add them, the groups (`apps`) after them.
# isort: off
from keryx.cli import main, serve, restart, tools, forget, diagnostics, migrate  # noqa: F401
from keryx.cli import integrations, approvals  # noqa: F401
from keryx.cli import tasks, memory, plugins, models, voice, service, pin, setup  # noqa: F401
from keryx.cli import config, auth, phone  # noqa: F401
# isort: on

__all__ = ["app", "renamed"]
