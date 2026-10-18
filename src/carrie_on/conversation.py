"""Chat identity, catalog metadata, and the state needed to resume work."""

from dataclasses import dataclass


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
    message_id: str | None
    turn_id: str
    model: str
    status: str
    busy: bool
    blocked: bool
