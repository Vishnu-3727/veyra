"""Tests for parse_amount: only finite numbers are valid money.

Non-finite input (inf/-inf/NaN) used to survive parsing and then poison every
downstream amount-difference computation, so it must fail validation (None)
exactly like a corrupt string.
"""
import math

import pytest

from app.normalization import parse_amount, parse_date


@pytest.mark.parametrize(
    "value",
    [
        float("inf"),
        float("-inf"),
        float("nan"),
        "inf",
        "-inf",
        "Infinity",
        "-Infinity",
        "INF",
        "nan",
        "NaN",
        "",
        "   ",
        None,
        "abc",
        "1.2.3",
        "none",
        "null",
    ],
)
def test_invalid_amounts_return_none(value):
    assert parse_amount(value) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1,234.50", 1234.5),
        ("\u20b91,234.50", 1234.5),
        ("1234.50", 1234.5),
        (1000, 1000.0),
        (1000.0, 1000.0),
        ("  1234.5  ", 1234.5),
        ("-500.25", -500.25),
        ("1234.567", 1234.57),
    ],
)
def test_valid_amounts_parse_to_expected_float(value, expected):
    result = parse_amount(value)
    assert result == expected
    assert math.isfinite(result)


def test_parse_date_rejects_garbage_without_raising():
    for value in (None, "", "   ", "abc", "nan", "null", "2026-13-45"):
        assert parse_date(value) is None
    assert parse_date("2026-09-05").isoformat() == "2026-09-05"
