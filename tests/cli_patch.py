"""Patching a name the command line uses. `keryx.cli` is a package, each module of which
imports what it uses by name, so a patch goes to every module that has the name — as one
patch of the single `cli.py` it replaced reached every use."""

import importlib
import pkgutil
from types import ModuleType

import pytest

import keryx.cli


def cli_modules() -> list[ModuleType]:
    """`keryx.cli` and every module in it."""
    return [keryx.cli, *(importlib.import_module(f"keryx.cli.{info.name}")
                         for info in pkgutil.iter_modules(keryx.cli.__path__))]


def patch_cli(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    """`monkeypatch.setattr` of `name` in each module of `keryx.cli` that has it."""
    found = [module for module in cli_modules() if hasattr(module, name)]
    assert found, f"no module of keryx.cli has {name}"
    for module in found:
        monkeypatch.setattr(module, name, value)
