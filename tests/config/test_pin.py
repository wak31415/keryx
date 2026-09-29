"""`spoken_digits`: a PIN as it was said, read back to the digits it stands for."""

import pytest

from jarvis.config import spoken_digits


@pytest.mark.parametrize(
    ("said", "digits"),
    [
        ("424242", "424242"),
        ("  424242 ", "424242"),
        ("424-242", "424242"),
        ("424–242", "424242"),
        ("424 — 242", "424242"),
        ("42 42 42", "424242"),
        ("4, 2, 4. 2, 4, 2", "424242"),
        ("four two four two four two", "424242"),
        ("FOUR-TWO-FOUR two four two", "424242"),
        ("one 2 three 4 five 6", "123456"),
        ("zero oh o nought 0", "00000"),
        ("four", "4"),
    ],
)
def test_separators_go_and_words_become_digits(said, digits):
    assert spoken_digits(said) == digits


@pytest.mark.parametrize(
    "said",
    [
        "",
        "   ",
        "- , .",
        "pin 424242",
        "424242 please",
        "4two4",
        "fourtwo",
        "424242#",
        "424*242",
        "٤٢٤٢٤٢",
        "４２４２４２",
    ],
)
def test_anything_but_digits_is_no_pin_at_all(said):
    """Nothing is picked out of the rest: one stray word and it is not a PIN."""
    assert spoken_digits(said) is None


def test_none_is_no_pin():
    assert spoken_digits(None) is None
