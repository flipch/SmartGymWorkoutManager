"""Typed failures. A feed that cannot be read raises; it never returns an empty value that looks like data."""


class HarnessError(Exception):
    """Base class for all harness errors."""


class SourceDown(HarnessError):
    """A data source could not be read (network, HTTP, or API-level error)."""

    def __init__(self, source: str, reason: str):
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


class SourceAuthError(SourceDown):
    """The source rejected our session (Speediance codes 90/91): re-login is the only fix."""


class SchemaDrift(SourceDown):
    """The source answered, but the payload no longer has the fields we depend on."""
