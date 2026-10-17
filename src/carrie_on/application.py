"""Add chats to the queue and read their current Codex and queue state."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID

from carrie_on.codex.client import AppServer, CodexConnections, Desktop
from carrie_on.codex.models import (
    Chat,
    ChatPage,
    ChatSummary,
    CodexProtocolError,
    CodexUnavailable,
    UsageSelectionError,
    UsageSnapshot,
)
from carrie_on.pagination import decode_cursor, encode_cursor
from carrie_on.ports import QueueStorage, StorageUnavailable
from carrie_on.queue import Eligibility, QueueEntry, QueueError, QueueSummary


@dataclass(frozen=True)
class Status:
    connected: bool
    usage: UsageSnapshot | None
    queue: QueueSummary | None
    usage_error: str | None
    queue_error: str | None


@dataclass(frozen=True)
class Readiness:
    id: UUID
    title: str
    eligibility: Eligibility


@dataclass(frozen=True)
class Diagnostic:
    account_id: str
    chat: Chat | None = None


def observe_eligibility(entry: QueueEntry, server: AppServer, desktop: Desktop) -> Eligibility:
    """Check quota and chat state to decide whether an entry can be sent.

    Check the local catalog before reading or opening the chat. If the chat has
    continued, look for the queued message before treating the entry as stale.
    """
    try:
        usage = server.usage().select(entry.limit_id)
    except UsageSelectionError as error:
        raise QueueError("quota_changed", str(error)) from error
    present = any(chat.id == str(entry.chat_id) for chat in server.list_all_chats(entry.title))
    chat = server.get_chat(str(entry.chat_id), desktop) if present else None
    accepted = False
    if chat is not None:
        accepted = chat.message_id == str(entry.id)
        if not accepted and not entry.matches_anchor(chat):
            accepted = server.has_message(str(entry.chat_id), str(entry.id))
    return entry.assess(usage, chat, accepted=accepted)


class QueueService:
    """Create queue entries and read queue status using current Codex data.

    Operations that need Codex data open their own connections. Enrollment reads
    current quota instead of reusing a status response. Storage rejects duplicate
    pending entries when saving, including concurrent enrollments for the same chat.
    """

    def __init__(self, store: QueueStorage, connections: CodexConnections):
        self.store = store
        self.connections = connections

    def read_usage(self) -> UsageSnapshot:
        with self.connections.app_server() as server:
            return server.usage()

    def resolve_chat(self, query: str) -> str:
        """Return a chat UUID from a UUID, a deep link, or a unique exact title."""
        if query.startswith("codex://threads/"):
            query = urlparse(query).path.strip("/")
        try:
            return str(UUID(query))
        except ValueError:
            pass
        with self.connections.app_server() as server:
            matches = [chat for chat in server.list_all_chats(query) if chat.title == query]
        if len(matches) != 1:
            raise QueueError(
                "invalid_chat", f"Found {len(matches)} exact title matches; select a chat UUID"
            )
        return matches[0].id

    def _validate_catalog(self, chat_id: str) -> None:
        with self.connections.app_server() as server:
            chat = server.get_chat_summary(chat_id)
            if not any(value.id == chat_id for value in server.list_all_chats(chat.title)):
                raise QueueError("chat_absent", "Chat is archived or absent from the local catalog")

    def enqueue(
        self, entry_id: UUID, chat_id: UUID, instructions: str, *, limit_id: str | None = None
    ) -> QueueEntry:
        """Create an entry with the chat's latest turn and current reset timestamps."""
        queued, _ = self.store.list_entries(chat_id=chat_id, limit=1)
        if queued:
            raise QueueError("chat_already_queued", "Chat is already queued")
        with (
            self.connections.app_server() as server,
            self.connections.desktop(open_if_needed=True) as desktop,
        ):
            chat = server.get_chat(
                str(chat_id),
                desktop,
                include_goal=False,
                before_open=lambda: self._validate_catalog(str(chat_id)),
            )
        if chat.thread_id != str(chat_id):
            raise QueueError("chat_mismatch", "Desktop returned a different chat")
        try:
            usage = self.read_usage().select(limit_id)
        except UsageSelectionError as error:
            raise QueueError("invalid_bucket", str(error)) from error
        entry = QueueEntry.enroll(entry_id, instructions, chat, usage, datetime.now(timezone.utc))
        return self.store.create_entry(entry)

    def list_chats(self, search: str = "", cursor: str | None = None, limit: int = 25) -> ChatPage:
        scope = ["chats", search, limit]
        upstream = decode_cursor(cursor, scope) if cursor else None
        if upstream is not None and not isinstance(upstream, str):
            raise QueueError("invalid_cursor", "Invalid chat cursor")
        with self.connections.app_server() as server:
            page = server.list_chats(search, cursor=upstream, limit=limit)
        next_cursor = encode_cursor(scope, page.next_cursor) if page.next_cursor else None
        return ChatPage(page.items, next_cursor)

    def list_queued_chats(self, cursor: str | None = None, limit: int = 50) -> ChatPage:
        """Read queued chat details from Codex, using saved titles if Codex is unavailable."""
        entries, next_cursor = self.store.list_entries(limit=limit, cursor=cursor)
        result = [
            ChatSummary(id=str(entry.chat_id), title=entry.title, cwd=None, last_activity=None)
            for entry in entries
        ]
        if not entries:
            return ChatPage((), next_cursor)
        try:
            with self.connections.app_server() as server:
                for index, entry in enumerate(entries):
                    try:
                        chat = server.get_chat_summary(str(entry.chat_id))
                        result[index] = replace(chat, title=chat.title or entry.title)
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
        """Return available status data with separate storage and Codex error messages.

        Unexpected exceptions propagate instead of producing partial status.
        """
        queue = usage = None
        queue_error = usage_error = None
        try:
            queue = self.store.get_queue_summary()
        except StorageUnavailable as error:
            queue_error = str(error)
        try:
            usage = self.read_usage()
        except (CodexUnavailable, CodexProtocolError) as error:
            usage_error = str(error)
        return Status(self.connected(), usage, queue, usage_error, queue_error)

    def doctor(self, chat_id: str | None = None) -> Diagnostic:
        """Check the connection and optional chat without opening it or starting a turn."""
        with self.connections.app_server() as server, self.connections.desktop() as desktop:
            usage = server.usage()
            return Diagnostic(
                usage.account_id, server.get_chat(chat_id, desktop) if chat_id else None
            )

    def inspect(
        self, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[Readiness], str | None]:
        """Check which entries can be sent without opening chats or changing the queue."""
        entries, next_cursor = self.store.list_entries(limit=limit, cursor=cursor)
        if not entries:
            return [], next_cursor
        with self.connections.app_server() as server, self.connections.desktop() as desktop:
            results = [
                Readiness(
                    entry.id,
                    entry.title,
                    observe_eligibility(entry, server, desktop),
                )
                for entry in entries
            ]
        return results, next_cursor
