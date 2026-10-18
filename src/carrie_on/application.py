"""Queue operations that depend on storage and Codex protocols."""

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID

from carrie_on.conversation import Chat, ChatPage, ChatSummary
from carrie_on.pagination import decode_cursor, encode_cursor
from carrie_on.ports import Codex, CodexUnavailable, QueueStorage, StorageUnavailable
from carrie_on.queue import Eligibility, EntryState, QueueEntry, QueueError, QueueSummary
from carrie_on.usage import UsageSelectionError, UsageSnapshot


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
    eligibility: Eligibility


@dataclass(frozen=True)
class Diagnostic:
    account_id: str
    chat: Chat | None = None


def observe_eligibility(
    entry: QueueEntry, codex: Codex, *, open_if_needed: bool = False
) -> Eligibility:
    """Reconcile an accepted send before treating a changed anchor as stale."""
    try:
        usage = codex.read_usage().select(entry.limit_id)
    except UsageSelectionError as error:
        raise QueueError("quota_changed", str(error)) from error
    chat = codex.get_chat(entry.chat_id, open_if_needed=open_if_needed)
    accepted = False
    if chat is not None:
        accepted = chat.message_id == str(entry.id)
        if not accepted and not entry.matches_anchor(chat):
            accepted = codex.has_message(entry.chat_id, entry.id)
    return entry.assess(usage, chat, accepted=accepted)


def resolve_chat(query: str, *, codex: Codex) -> UUID:
    """Resolve a UUID, deep link, or unique exact title."""
    if query.startswith("codex://threads/"):
        query = urlparse(query).path.strip("/")
    try:
        return UUID(query)
    except ValueError:
        pass
    matches: list[ChatSummary] = []
    cursor = None
    seen: set[str] = set()
    while True:
        page = list_chats(query, cursor=cursor, limit=100, codex=codex)
        matches.extend(chat for chat in page.items if chat.title == query)
        cursor = page.next_cursor
        if cursor is None:
            break
        if cursor in seen:
            raise CodexUnavailable("Codex returned a repeated catalog cursor")
        seen.add(cursor)
    if len(matches) != 1:
        raise QueueError(
            "invalid_chat", f"Found {len(matches)} exact title matches; select a chat UUID"
        )
    return UUID(matches[0].id)


def enqueue(
    entry_id: UUID,
    chat_id: UUID,
    instructions: str,
    *,
    limit_id: str | None = None,
    store: QueueStorage,
    codex: Codex,
) -> QueueEntry:
    """Capture the current anchor and quota windows, then save the queued instruction."""
    queued, _ = store.list_entries(chat_id=chat_id, limit=1)
    if queued:
        raise QueueError("chat_already_queued", "Chat is already queued")
    chat = codex.get_chat(chat_id, open_if_needed=True, include_goal=False)
    if chat is None:
        raise QueueError("chat_absent", "Chat is archived or absent from the local catalog")
    if chat.thread_id != str(chat_id):
        raise QueueError("chat_mismatch", "Desktop returned a different chat")
    try:
        usage = codex.read_usage().select(limit_id)
    except UsageSelectionError as error:
        raise QueueError("invalid_bucket", str(error)) from error
    entry = QueueEntry.enroll(entry_id, instructions, chat, usage, datetime.now(timezone.utc))
    return store.create_entry(entry)


def list_chats(
    search: str = "", cursor: str | None = None, limit: int = 25, *, codex: Codex
) -> ChatPage:
    scope = ["chats", search, limit]
    upstream = decode_cursor(cursor, scope) if cursor else None
    if upstream is not None and not isinstance(upstream, str):
        raise QueueError("invalid_cursor", "Invalid chat cursor")
    page = codex.list_chats(search, cursor=upstream, limit=limit)
    if page.next_cursor is not None and page.next_cursor == upstream:
        raise CodexUnavailable("Codex returned a repeated catalog cursor")
    next_cursor = encode_cursor(scope, page.next_cursor) if page.next_cursor else None
    return ChatPage(page.items, next_cursor)


def list_queued_chats(
    cursor: str | None = None, limit: int = 50, *, store: QueueStorage, codex: Codex
) -> ChatPage:
    """Return available catalog metadata; missing metadata stays empty."""
    entries, next_cursor = store.list_entries(limit=limit, cursor=cursor)
    if not entries:
        return ChatPage((), next_cursor)
    try:
        summaries = codex.list_chat_summaries(tuple(entry.chat_id for entry in entries))
    except CodexUnavailable:
        summaries = ()
    by_id = {chat.id: chat for chat in summaries}
    return ChatPage(
        tuple(
            by_id.get(
                str(entry.chat_id),
                ChatSummary(id=str(entry.chat_id), title=None, cwd=None, last_activity=None),
            )
            for entry in entries
        ),
        next_cursor,
    )


def status(*, store: QueueStorage, codex: Codex) -> Status:
    queue = usage = None
    queue_error = usage_error = None
    try:
        queue = store.get_queue_summary()
    except StorageUnavailable as error:
        queue_error = str(error)
    try:
        usage = codex.read_usage()
    except CodexUnavailable as error:
        usage_error = str(error)
    return Status(codex.connected(), usage, queue, usage_error, queue_error)


def doctor(chat_id: UUID | None = None, *, codex: Codex) -> Diagnostic:
    """Check Codex without opening chats or starting a turn."""
    usage = codex.read_usage()
    if not codex.connected():
        raise CodexUnavailable("Codex desktop is unavailable")
    chat = codex.get_chat(chat_id) if chat_id else None
    if chat_id and chat is None:
        raise QueueError("chat_absent", "Chat is archived or absent from the local catalog")
    return Diagnostic(usage.account_id, chat)


def inspect(
    limit: int = 50, cursor: str | None = None, *, store: QueueStorage, codex: Codex
) -> tuple[list[Readiness], str | None]:
    """Keep entry-specific failures in the page without mutating queue state."""
    entries, next_cursor = store.list_entries(limit=limit, cursor=cursor)
    results = []
    for entry in entries:
        try:
            decision = observe_eligibility(entry, codex)
        except QueueError as error:
            decision = Eligibility("failed", str(error))
        results.append(Readiness(entry.id, decision))
    return results, next_cursor


def get_entry(entry_id: UUID, *, store: QueueStorage) -> QueueEntry:
    entry = store.get_entry(entry_id)
    if entry is None:
        raise QueueError("not_found", "Queue entry not found")
    return entry


def list_entries(
    *,
    store: QueueStorage,
    states: tuple[EntryState, ...] | None,
    chat_id: UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[QueueEntry], str | None]:
    return store.list_entries(states=states, chat_id=chat_id, limit=limit, cursor=cursor)


def update_instructions(
    entry_id: UUID, instructions: str, *, revision: int, store: QueueStorage
) -> QueueEntry:
    return store.update_instructions(entry_id, instructions, revision=revision)


def remove_entry(entry_id: UUID, *, store: QueueStorage) -> QueueEntry:
    return store.update_entry_state(entry_id, "cancelled")
