"""Parse Codex responses into the chat and quota records the queue uses."""

import math
from dataclasses import dataclass
from typing import Any


class CodexUnavailable(Exception):
    """Report a connection failure. Codex may have accepted a write before it failed."""


class CodexProtocolError(Exception):
    """Report a Codex response that the application cannot parse or use."""


class UsageSelectionError(Exception):
    """Report a missing quota bucket or ask the caller to choose between buckets."""


def object_value(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CodexProtocolError(f"Invalid {description}")
    return value


def text_value(value: Any, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise CodexProtocolError(f"Missing {description}")
    return value


def finite_number(value: Any, description: str) -> float:
    """Validate JSON numbers without overflowing on arbitrary-size integers."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CodexProtocolError(f"Invalid {description}")
    try:
        number = float(value)
    except OverflowError as error:
        raise CodexProtocolError(f"Invalid {description}") from error
    if not math.isfinite(number):
        raise CodexProtocolError(f"Invalid {description}")
    return number


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
                raise UsageSelectionError("Choose --limit-id from: " + names)
            return self.buckets[0]
        for bucket in self.buckets:
            if bucket.limit_id == limit_id:
                return bucket
        raise UsageSelectionError(f"Usage bucket {limit_id!r} is unavailable")

    @classmethod
    def parse(cls, response: Any) -> "UsageSnapshot":
        response = object_value(response, "quota response")
        account_id = text_value(response.get("accountId"), "Codex account ID")
        ordinary = response.get("ordinaryUsageAllowed")
        if type(ordinary) is not bool:
            raise CodexProtocolError("Missing or invalid usage availability flag")
        buckets = response.get("rateLimitsByLimitId")
        if buckets is None:
            legacy = object_value(response.get("rateLimits"), "legacy usage bucket")
            buckets = {legacy.get("limitId") or "codex": legacy}
        buckets = object_value(buckets, "usage buckets")
        if not buckets:
            raise CodexProtocolError("Codex did not report usage buckets")
        parsed = []
        for limit_id, raw_bucket in buckets.items():
            limit_id = text_value(limit_id, "quota bucket ID")
            bucket = object_value(raw_bucket, "usage bucket")
            windows = []
            for name in ("primary", "secondary"):
                raw = bucket.get(name)
                if raw is None:
                    continue
                raw = object_value(raw, f"{name} quota window")
                used = finite_number(raw.get("usedPercent"), f"{name} usage percentage")
                reset = raw.get("resetsAt")
                duration = raw.get("windowDurationMins")
                if (
                    used < 0
                    or type(reset) is not int
                    or reset <= 0
                    or reset >= 2**63
                    or (duration is not None and (type(duration) is not int or duration <= 0))
                ):
                    raise CodexProtocolError(f"Incomplete {name} quota information")
                windows.append(
                    Window(name=name, used=used, reset_at=reset, duration_minutes=duration)
                )
            if not windows:
                raise CodexProtocolError("No timed usage windows available")
            spend_control = bucket.get("spendControlReached", False)
            if not isinstance(spend_control, bool):
                raise CodexProtocolError("Invalid spend control availability flag")
            reached_type = bucket.get("rateLimitReachedType")
            if reached_type is not None and not isinstance(reached_type, str):
                raise CodexProtocolError("Invalid rate limit reached type")
            parsed.append(
                Usage(
                    account_id=account_id,
                    limit_id=limit_id,
                    windows=tuple(windows),
                    allowed=ordinary and not spend_control and not reached_type,
                    spend_control_reached=spend_control,
                    reached_type=reached_type,
                )
            )
        return cls(account_id=account_id, ordinary_usage_allowed=ordinary, buckets=tuple(parsed))


@dataclass(frozen=True, kw_only=True)
class ChatSummary:
    id: str
    title: str | None
    cwd: str | None
    last_activity: float | None


@dataclass(frozen=True)
class ChatPage:
    items: tuple[ChatSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, kw_only=True)
class Chat:
    """Record the latest accepted turn and the chat state needed for dispatch."""

    thread_id: str
    title: str
    message_id: str | None
    turn_id: str
    model: str
    status: str
    busy: bool
    blocked: bool
