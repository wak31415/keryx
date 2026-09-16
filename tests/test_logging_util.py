"""Tests for `jarvis.logging_util`: what a phone number looks like once it is written down."""

import logging

import pytest

from jarvis.logging_util import mask_number


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("+15551234567", "…4567"),
        ("+442079460123", "…0123"),  # a longer, non-NANP number
        # Short enough that masking would leave nothing; there is nothing to hide either.
        ("911", "911"),
        ("1234", "1234"),
        (None, "nobody"),
        ("", "nobody"),
    ],
)
def test_only_the_last_four_digits_survive(number, expected):
    assert mask_number(number) == expected


def test_a_masked_number_is_still_enough_to_tell_two_callers_apart():
    assert mask_number("+15551234567") != mask_number("+15559999999")


def test_the_full_number_is_not_in_the_masked_form(caplog):
    number = "+15551234567"

    with caplog.at_level(logging.INFO):
        logging.getLogger("jarvis.test").info("a call from %s", mask_number(number))

    assert number not in caplog.text
    assert "…4567" in caplog.text
