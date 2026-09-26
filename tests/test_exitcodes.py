"""aria2c exit status -> curl exit status translation."""

from __future__ import annotations

import pytest

from aria2curl.exitcodes import aria2_to_curl, curl_status_name


@pytest.mark.parametrize(
    "aria2,curl",
    [
        (0, 0),
        (1, 1),
        (2, 28),
        (3, 22),
        (4, 22),
        (5, 28),
        (6, 7),
        (7, 18),
        (8, 36),
        (9, 23),
        (13, 23),
        (16, 23),
        (17, 23),
        (18, 23),
        (19, 6),
        (23, 47),
        (24, 67),
        (27, 3),
        (28, 2),
        (32, 22),
        (99, 1),
    ],
)
def test_mapping(aria2: int, curl: int) -> None:
    assert aria2_to_curl(aria2) == curl


def test_signal_death_becomes_128_plus_signal() -> None:
    assert aria2_to_curl(-2) == 130
    assert aria2_to_curl(-15) == 143


def test_status_names() -> None:
    assert curl_status_name(0) == "OK"
    assert "timed out" in curl_status_name(28)
    assert "login" in curl_status_name(67)
    assert curl_status_name(12345) == "error"
