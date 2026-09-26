"""Exception types used across aria2curl."""

from __future__ import annotations


class Aria2CurlError(Exception):
    """Base class for every error raised on purpose by aria2curl."""


class UsageError(Aria2CurlError):
    """Bad usage of the aria2curl command line itself (not of curl's)."""


class ConfigError(Aria2CurlError):
    """Invalid configuration key or value."""


class FallbackNeeded(Aria2CurlError):
    """The curl command line cannot be translated faithfully to aria2.

    Raising this aborts the aria2 path and hands the *original* argv to curl.
    ``reason`` is a short human readable explanation shown to the user (and in
    ``--acurl-explain`` output).
    """

    def __init__(self, reason: str, *, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.detail:
            return f"{self.reason} ({self.detail})"
        return self.reason


class Aria2StartupError(Aria2CurlError):
    """aria2c could not be started (missing binary, immediate crash, ...)."""
