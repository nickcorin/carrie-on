"""Enrollment and read operations over typed Codex observations and queue state."""

import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from urllib.parse import urlparse
from uuid import UUID

from carrie_on.codex.client import AppServer, CodexConnections, Desktop
from carrie_on.codex.models import (
    Chat,
    ChatPage,
    ChatSummary,
    CodexProtocolError,
    CodexUnavailable,
    UsageObservation,
    UsageSelectionError,
    UsageSnapshot,
)
from carrie_on.queue import Eligibility, EnqueueCommand, QueueEntry, QueueError, QueueItem, evaluate
from carrie_on.storage import QueueStore, QueueSummary, cursor_decode, cursor_encode


@dataclass(frozen=True)
class Status:
    connected: bool
    usage: UsageSnapshot | None
    queue: QueueSummary | None
    usage_error: str | None
    queue_error: str | None


@dataclass(frozen=True)
class Readiness:
    id: str
    title: str
    eligibility: Eligibility


@dataclass(frozen=True)
class Diagnostic:
    account_id: str
    chat: Chat | None = None


def observe_eligibility(
    item: QueueItem, server: AppServer, desktop: Desktop, now: float
) -> Eligibility:
    """Read quota, check catalog before any opening, then evaluate live state."""
    try:
        usage = server.usage().select(item.limit_id)
    except UsageSelectionError as error:
        raise QueueError("quota_changed", str(error)) from error
    present = any(chat.id == item.chat_id for chat in server.threads(item.title))
    chat = server.snapshot(item.chat_id, desktop, receipt_id=item.id) if present else None
    return evaluate(item, usage, chat, now)


class QueueService:
    """Enrollment owns a short-lived quota cache; dispatch always reads fresh state.

    Each operation opens independent connections. No network I/O occurs inside a
    queue transaction. The store rechecks mutable membership at commit time.
    """

    def __init__(self, store: QueueStore, connections: CodexConnections):
        self.store = store
        self.connections = connections
        self.usage_observations: dict[str, UsageObservation] = {}
        self.usage_observed_at = 0.0
        self.usage_lock = threading.Lock()

    def read_usage(self) -> UsageSnapshot:
        """Cache only successful, ordered observations, exclusively for enrollment."""
        observed_at = time.time()
        observations = {}
        try:
            with self.connections.app_server() as server:
                usage = server.usage()
            observations = {
                bucket.limit_id: UsageObservation(bucket, observed_at) for bucket in usage.buckets
            }
            return usage
        finally:
            with self.usage_lock:
                if observed_at >= self.usage_observed_at:
                    self.usage_observations = observations
                    self.usage_observed_at = observed_at

    def _enrollment_usage(self, limit_id: str | None) -> UsageObservation | None:
        with self.usage_lock:
            if limit_id is None and len(self.usage_observations) == 1:
                return next(iter(self.usage_observations.values()))
            return self.usage_observations.get(limit_id) if limit_id is not None else None

    def resolve_chat(self, query: str) -> str:
        """Resolve a UUID/deep link or an exact, unambiguous catalog title."""
        if query.startswith("codex://threads/"):
            query = urlparse(query).path.strip("/")
        try:
            return str(UUID(query))
        except ValueError:
            pass
        with self.connections.app_server() as server:
            matches = [chat for chat in server.threads(query) if chat.title == query]
        if len(matches) != 1:
            raise QueueError(
                "invalid_chat", f"Found {len(matches)} exact title matches; select a chat UUID"
            )
        return matches[0].id

    def _validate_catalog(self, chat_id: str) -> None:
        with self.connections.app_server() as server:
            chat = server.read_chat(chat_id)
            if not any(value.id == chat_id for value in server.threads(chat.title)):
                raise QueueError("chat_absent", "Chat is archived or absent from the local catalog")

    def enqueue(self, command: EnqueueCommand) -> tuple[QueueEntry, bool]:
        """Replay original identity or enroll the latest live turn with available quota metadata."""
        existing = self.store.replay(command)
        if existing:
            return existing, False
        queued, _ = self.store.page(state="queued", chat_id=command.chat_id, limit=1)
        if queued:
            raise QueueError("chat_already_queued", "Chat is already queued")
        with self.connections.app_server() as server, self.connections.desktop(open_if_needed=True) as desktop:
            chat = server.snapshot(
                command.chat_id, desktop, include_goal=False,
                before_open=lambda: self._validate_catalog(command.chat_id)
            )
        if chat.thread_id != command.chat_id:
            raise QueueError("chat_mismatch", "Desktop returned a different chat")
        observed = self._enrollment_usage(command.limit_id)
        if observed and observed.usable_for_enrollment(time.time()):
            usage = observed.usage
        else:
            with self.connections.app_server() as server:
                try:
                    usage = server.usage().select(command.limit_id)
                except UsageSelectionError as error:
                    raise QueueError("invalid_bucket", str(error)) from error
        item = QueueItem.enroll(command, chat, usage, time.time())
        return self.store.enqueue(item, command)

    def chats(self, search: str = "", cursor: str | None = None, limit: int = 25) -> ChatPage:
        scope = ["chats", search, limit]
        upstream = cursor_decode(cursor, scope) if cursor else None
        if upstream is not None and not isinstance(upstream, str):
            raise QueueError("invalid_cursor", "Invalid chat cursor")
        with self.connections.app_server() as server:
            page = server.thread_page(search, cursor=upstream, limit=limit)
        next_cursor = cursor_encode(scope, page.next_cursor) if page.next_cursor else None
        return ChatPage(page.items, next_cursor)

    def queued_chats(self, cursor: str | None = None, limit: int = 50) -> ChatPage:
        """Return live activity when available and saved enrollment titles offline."""
        entries, next_cursor = self.store.page(limit=limit, cursor=cursor)
        result = [
            ChatSummary(id=entry.item.chat_id, title=entry.item.title, cwd=None, last_activity=None)
            for entry in entries
        ]
        if not entries:
            return ChatPage((), next_cursor)
        try:
            with self.connections.app_server() as server:
                for index, entry in enumerate(entries):
                    try:
                        chat = server.read_chat(entry.item.chat_id)
                        result[index] = replace(chat, title=chat.title or entry.item.title)
                    except (CodexUnavailable, CodexProtocolError):
                        pass
        except (CodexUnavailable, CodexProtocolError):
            pass
        return ChatPage(tuple(result), next_cursor)

    def connected(self) -> bool:
        try:
            with self.connections.desktop(timeout=3):
                return True
        except (CodexUnavailable, CodexProtocolError):
            return False

    def status(self) -> Status:
        """Read independent menu sections; an upstream outage cannot hide the queue."""
        queue = usage = None
        queue_error = usage_error = None
        try:
            queue = self.store.summary()
        except sqlite3.Error as error:
            queue_error = str(error)
        try:
            usage = self.read_usage()
        except (CodexUnavailable, CodexProtocolError) as error:
            usage_error = str(error)
        return Status(self.connected(), usage, queue, usage_error, queue_error)

    def doctor(self, chat_id: str | None = None) -> Diagnostic:
        """Read connection/turn diagnostics without opening a chat or starting a turn."""
        with self.connections.app_server() as server, self.connections.desktop() as desktop:
            usage = server.usage()
            return Diagnostic(usage.account_id, server.snapshot(chat_id, desktop) if chat_id else None)

    def inspect(
        self, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Readiness], str | None]:
        """Read-only readiness inspection; no opening chats, claiming, or sending."""
        entries, next_cursor = self.store.page(state="queued", limit=limit, cursor=cursor)
        if not entries:
            return [], next_cursor
        with self.connections.app_server() as server, self.connections.desktop() as desktop:
            results = [
                Readiness(
                    entry.item.id,
                    entry.item.title,
                    observe_eligibility(entry.item, server, desktop, time.time()),
                )
                for entry in entries
            ]
        return results, next_cursor
