"""The assistant's name, and the voice that comes with it."""

import pytest
from pydantic import ValidationError

from keryx.config import Settings
from keryx.persona import DEFAULT_VOICE, PERSONAS, voice_for


def _settings(**values) -> Settings:
    return Settings(_env_file=None, openai_api_key="test", **values)


def test_each_built_in_persona_brings_its_own_voice():
    assert voice_for("Lyra") == PERSONAS["lyra"].voice
    assert voice_for("Jarvis") == "cedar"


def test_a_persona_is_matched_whatever_the_case():
    assert voice_for("  JARVIS ") == "cedar"


def test_any_other_name_speaks_in_the_default_voice():
    assert voice_for("Ada") == DEFAULT_VOICE


def test_an_upgraded_install_answers_as_lyra_in_lyras_voice():
    settings = _settings()

    assert settings.assistant_name == "Lyra"
    assert settings.voice == PERSONAS["lyra"].voice


def test_choosing_jarvis_brings_jarvis_voice_back_without_a_second_setting():
    assert _settings(assistant_name="Jarvis").voice == "cedar"


def test_an_explicit_voice_beats_the_persona():
    assert _settings(assistant_name="Jarvis", openai_voice="ash").voice == "ash"


def test_a_persona_is_spelled_its_own_way():
    assert _settings(assistant_name="jarvis").assistant_name == "Jarvis"


@pytest.mark.parametrize("name", ["Mary-Jane", "O'Neil", "Zoë", "Big Al"])
def test_a_free_form_name_is_kept_as_given(name):
    assert _settings(assistant_name=name).assistant_name == name


def test_runs_of_spaces_in_a_name_are_collapsed():
    assert _settings(assistant_name="  Big   Al ").assistant_name == "Big Al"


@pytest.mark.parametrize("name", ["", "R2D2", "{owner}", "x" * 33, "-Lyra"])
def test_a_name_the_prompt_cannot_say_is_refused(name):
    with pytest.raises(ValidationError, match="letters, spaces, apostrophes or hyphens"):
        _settings(assistant_name=name)


@pytest.mark.parametrize("name", ["Claude", "codex"])
def test_a_coding_agents_name_is_refused(name):
    """The prompt hands work to Claude; an assistant called Claude would hand it to itself."""
    with pytest.raises(ValidationError, match="coding agent's name"):
        _settings(assistant_name=name)
