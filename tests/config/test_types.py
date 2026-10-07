"""`field_type`: the control a setting wants, read off its annotation."""

from keryx.config.settings import Settings
from keryx.config.types import TYPES, field_type


def test_a_literal_is_a_choice_of_its_values_in_order():
    assert field_type("vad_mode") == ("choice", ["server", "semantic"])


def test_a_literal_behind_an_alias_is_a_choice_too():
    kind, choices = field_type("agent_backend")

    assert kind == "choice"
    assert choices == ["claude", "codex", "local"]


def test_a_bool_is_a_bool_not_an_int():
    assert field_type("sms_enabled") == ("bool", None)


def test_an_int_and_a_float():
    assert field_type("port") == ("int", None)
    assert field_type("subagent_max_budget_usd") == ("float", None)


def test_a_path_with_or_without_none():
    assert field_type("data_dir") == ("path", None)
    assert field_type("keryx_checkout") == ("path", None)


def test_a_list_of_plain_strings_has_no_choices():
    assert field_type("allowed_callers") == ("list", None)


def test_a_list_of_literals_is_a_list_whose_choices_are_its_values_in_order():
    """`AGENTS_ENABLED` is `list[Literal["claude", "codex", "local"]]`: the type stays
    `"list"` (it is still comma-separated at the command line), but its element type's
    values ride along as `choices`, same as a plain `Literal` would."""
    assert field_type("agents_enabled") == ("list", ["claude", "codex", "local"])


def test_an_optional_string_and_a_secret_are_strings():
    assert field_type("openai_api_key") == ("string", None)
    assert field_type("assistant_name") == ("string", None)


def test_a_mapping_is_written_as_a_string():
    """`PROJECTS` is set as JSON text: there is no control of its own for it."""
    assert field_type("projects") == ("string", None)


def test_every_setting_has_one_of_the_seven_types():
    assert TYPES == ("bool", "int", "float", "string", "list", "choice", "path")
    for name in Settings.model_fields:
        kind, choices = field_type(name)
        assert kind in TYPES, name
        # A `choice` always has its values; everything but a `list` never does — a `list`
        # has them only when its element type is itself a `Literal` (`AGENTS_ENABLED`).
        if kind == "choice":
            assert choices is not None, name
        elif kind != "list":
            assert choices is None, name
        assert choices is None or all(isinstance(choice, str) for choice in choices), name
