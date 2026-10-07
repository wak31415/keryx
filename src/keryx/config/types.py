"""What kind of value a setting takes, for a program that draws a control for it.

`keryx config list --json` carries it on every row: the Mac app's Configuration screen picks
a switch, a number field, a menu or a path from it, and never parses a type out of Python.
"""

import types
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from keryx.config.settings import Settings

#: Every kind a setting can be, and the only ones `field_type` returns.
TYPES = ("bool", "int", "float", "string", "list", "choice", "path")

_LISTS = (list, tuple, set, frozenset)


def _unwrap_optional(annotation: Any) -> Any:
    """`X | None` (or `Optional[X]`) is X: blank is how every setting says "unset"."""
    if get_origin(annotation) in (Union, types.UnionType):
        rest = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(rest) == 1:
            return rest[0]
    return annotation


def field_type(name: str) -> tuple[str, list[str] | None]:
    """`(kind, choices)` for a `Settings` field: one of `TYPES`, and a `choice`'s values.

    A mapping, a secret and anything unforeseen are `string`, since that is how each is
    typed at `keryx config set`. A `list` whose element type is itself a `Literal`
    (`AGENTS_ENABLED`: `list[Literal["claude", "codex", "local"]]`) carries those values as
    `choices` too, in declaration order — the type stays `list`, since it is still
    comma-separated at the command line; every other list keeps `choices: None`.
    """
    annotation = _unwrap_optional(Settings.model_fields[name].annotation)
    origin = get_origin(annotation)
    if annotation is bool:  # before int: a bool is an int to Python
        return "bool", None
    if annotation is int:
        return "int", None
    if annotation is float:
        return "float", None
    if origin is Literal:
        return "choice", [str(choice) for choice in get_args(annotation)]
    if annotation in _LISTS or origin in _LISTS:
        args = get_args(annotation)
        if len(args) == 1 and get_origin(args[0]) is Literal:
            return "list", [str(choice) for choice in get_args(args[0])]
        return "list", None
    if isinstance(annotation, type) and issubclass(annotation, Path):
        return "path", None
    return "string", None
