"""Validated Codex observations shared by policy and application operations."""

import math
from dataclasses import dataclass
from typing import Any


class CodexUnavailable(Exception):
    """A connection failed; a write may have been accepted before the failure."""


class CodexProtocolError(Exception):
    """Codex returned an unsupported or incomplete observation."""


class UsageSelectionError(Exception):
    """The requested quota bucket is missing or requires explicit selection."""


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
    """A usage window; reset_at is Unix seconds and duration_minutes may be absent."""

    name: str
    used: float
    reset_at: int
    duration_minutes: int | None = None


@dataclass(frozen=True)
class Usage:
    """Validated quota for an account and a selected limit bucket."""

    account_id: str
    limit_id: str
    windows: tuple[Window, ...]
    allowed: bool
    spend_control_reached: bool = False
    reached_type: str | None = None


@dataclass(frozen=True)
class UsageSnapshot:
    """Complete normalized account quota, including the supported legacy format."""

    account_id: str
    ordinary_usage_allowed: bool
    buckets: tuple[Usage, ...]

    def select(self, limit_id: str | None = None) -> Usage:
        """Select a named bucket, or the sole bucket; never guess between buckets."""
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


@dataclass(frozen=True)
class UsageObservation:
    """Recent quota may anchor enrollment, but never authorize dispatch."""

    usage: Usage
    observed_at: float

    def usable_for_enrollment(self, now: float) -> bool:
        return 0 <= now - self.observed_at <= 60 and all(
            window.reset_at > now for window in self.usage.windows
        )


@dataclass(frozen=True, kw_only=True)
class ChatSummary:
    id: str
    title: str | None
    cwd: str | None
    last_activity: float | None

    @classmethod
    def parse(cls, raw: Any) -> "ChatSummary":
        raw = object_value(raw, "chat summary")
        title = raw.get("name") or raw.get("preview")
        cwd, updated = raw.get("cwd"), raw.get("updatedAt")
        if updated is not None:
            updated = finite_number(updated, "chat activity timestamp")
        if (title is not None and not isinstance(title, str)) or (
            cwd is not None and not isinstance(cwd, str)
        ):
            raise CodexProtocolError("Invalid chat summary fields")
        return cls(
            id=text_value(raw.get("id"), "chat ID"), title=title, cwd=cwd, last_activity=updated
        )


@dataclass(frozen=True)
class ChatPage:
    items: tuple[ChatSummary, ...]
    next_cursor: str | None

    @classmethod
    def parse(cls, raw: Any) -> "ChatPage":
        raw = object_value(raw, "chat page")
        items, cursor = raw.get("data"), raw.get("nextCursor")
        if not isinstance(items, list) or (cursor is not None and not isinstance(cursor, str)):
            raise CodexProtocolError("Invalid chat page")
        return cls(tuple(ChatSummary.parse(item) for item in items), cursor)


@dataclass(frozen=True, kw_only=True)
class Chat:
    """Live desktop state relevant to continuing an enrolled chat."""

    thread_id: str
    title: str
    message_id: str
    model: str
    status: str
    busy: bool
    blocked: bool
    quota_failed: bool
    seen_message_ids: frozenset[str]

    @classmethod
    def parse(cls, snapshot: Any) -> "Chat":
        """Read the latest accepted/provisional turn from either supported history."""
        snapshot = object_value(snapshot, "desktop snapshot")
        thread_id = text_value(snapshot.get("id"), "desktop chat ID")
        message_id = text_value(
            snapshot.get("latestTurnStartMessageId"),
            "latest turn message ID; open the chat in Codex first",
        )
        model = text_value(snapshot.get("latestModel"), "latest model")
        turns = snapshot.get("turns", [])
        history = object_value(snapshot.get("turnHistory") or {}, "turn history")
        if history.get("kind") == "canonical":
            history = object_value(history.get("history"), "canonical turn history")
            turns = list(object_value(history.get("entitiesByKey"), "turn entities").values())
        if not isinstance(turns, list):
            raise CodexProtocolError("Invalid turn list")
        seen_ids = set()
        latest = []
        for raw in turns:
            turn = object_value(raw, "turn")
            params = object_value(turn.get("params") or {}, "turn parameters")
            client_id = params.get("clientUserMessageId")
            turn_id = turn.get("turnId")
            if client_id is not None and not isinstance(client_id, str):
                raise CodexProtocolError("Invalid turn message ID")
            if turn_id is not None and not isinstance(turn_id, str):
                raise CodexProtocolError("Invalid accepted turn ID")
            # Provisional messages without an accepted turn ID are not receipts.
            if turn_id and isinstance(client_id, str):
                seen_ids.add(client_id)
            if client_id == message_id:
                latest.append(turn)
        if len(latest) != 1:
            raise CodexProtocolError("Cannot identify the latest desktop turn; refusing to guess")
        turn = latest[0]
        status = turn.get("status")
        if not isinstance(status, str) or status not in {
            "inProgress",
            "completed",
            "failed",
            "interrupted",
        }:
            raise CodexProtocolError(f"Unsupported turn status: {status!r}")
        error = object_value(turn.get("error") or {}, "turn error")
        runtime = object_value(snapshot.get("threadRuntimeStatus") or {}, "runtime status")
        goal = object_value(snapshot.get("threadGoal") or {}, "goal")
        runtime_type, goal_status = runtime.get("type"), goal.get("status")
        title = snapshot.get("title")
        for value, description in (
            (runtime_type, "runtime type"),
            (goal_status, "goal status"),
            (title, "chat title"),
        ):
            if value is not None and not isinstance(value, str):
                raise CodexProtocolError(f"Invalid {description}")
        blocked = (
            bool(snapshot.get("requests"))
            or bool(snapshot.get("externalRequests"))
            or goal_status in {"paused", "budgetLimited", "blocked"}
            or runtime_type not in {"active", "idle", "systemError"}
            or snapshot.get("resumeState") != "resumed"
        )
        return cls(
            thread_id=thread_id,
            title=title or thread_id,
            message_id=message_id,
            model=model,
            status=status,
            busy=runtime_type == "active" or status == "inProgress",
            blocked=blocked,
            quota_failed=error.get("codexErrorInfo") == "usageLimitExceeded",
            seen_message_ids=frozenset(seen_ids),
        )
