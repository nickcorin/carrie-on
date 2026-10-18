"""Quota windows and account usage used when enrolling and dispatching work."""

from dataclasses import dataclass


class UsageSelectionError(Exception):
    """The requested quota bucket is missing or ambiguous."""


@dataclass(frozen=True)
class Window:
    """Record usage for a quota window.

    reset_at is a Unix timestamp in seconds. duration_minutes is None when Codex
    does not report the window's length.
    """

    name: str
    used: float
    reset_at: int
    duration_minutes: int | None = None


@dataclass(frozen=True)
class Usage:
    """Record quota usage and permission to send for an account and quota bucket."""

    account_id: str
    limit_id: str
    windows: tuple[Window, ...]
    allowed: bool
    spend_control_reached: bool = False
    reached_type: str | None = None


@dataclass(frozen=True)
class UsageSnapshot:
    """Store an account's quota buckets after parsing the Codex usage response."""

    account_id: str
    ordinary_usage_allowed: bool
    buckets: tuple[Usage, ...]

    def select(self, limit_id: str | None = None) -> Usage:
        """Return the named bucket, or the only bucket if no name was supplied.

        Raise UsageSelectionError if the named bucket does not exist or no name
        was supplied and the snapshot does not contain exactly one bucket.
        """
        if limit_id is None:
            if len(self.buckets) != 1:
                names = ", ".join(bucket.limit_id for bucket in self.buckets)
                raise UsageSelectionError("Choose a quota bucket from: " + names)
            return self.buckets[0]
        for bucket in self.buckets:
            if bucket.limit_id == limit_id:
                return bucket
        raise UsageSelectionError(f"Usage bucket {limit_id!r} is unavailable")
