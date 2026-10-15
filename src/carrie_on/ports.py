"""Define the storage operations the application uses without exposing database types."""

from pathlib import Path
from typing import Protocol
from uuid import UUID

from carrie_on.queue import DeliveryAttempt, EntryState, QueueEntry, QueueSummary


class StorageUnavailable(Exception):
    """Report a storage failure. A write may have succeeded before its reply was lost."""


class QueueStorage(Protocol):
    """Read and change queue entries and delivery attempts. Each operation is atomic."""

    def get_entry(self, entry_id: UUID) -> QueueEntry | None:
        """Return the entry with this UUID, or None if it does not exist."""
        ...

    def list_entries(
        self,
        *,
        states: tuple[EntryState, ...] | None = ("queued", "failed"),
        chat_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[QueueEntry], str | None]:
        """List entries ordered by queued_at, then id, with a page size of 1 to 100.

        Pass states=None to include every state. Reuse cursors only with the same
        filters and page size. Each call reads current data, so entries may change
        between pages.
        """
        ...

    def get_queue_summary(self) -> QueueSummary:
        """Count entries by state and active deliveries from the same storage read."""
        ...

    def create_entry(self, entry: QueueEntry) -> QueueEntry:
        """Insert an entry and return the saved record.

        Raise QueueError with entry_exists if its UUID already exists. Each chat
        can have at most one queued or failed entry. A duplicate pending entry
        raises QueueError with chat_already_queued.
        """
        ...

    def update_instructions(
        self, entry_id: UUID, instructions: str, *, revision: int
    ) -> QueueEntry:
        """Edit a pending entry with no active delivery if its revision still matches.

        Reject a conflicting revision without changing the entry.
        """
        ...

    def update_entry_state(
        self, entry_id: UUID, state: EntryState, *, error: str = ""
    ) -> QueueEntry:
        """Change the entry's state and error together without changing its delivery attempts."""
        ...

    def create_delivery_attempt(self) -> DeliveryAttempt | None:
        """Create an attempt for the next pending entry that has no active attempt.

        Return None if no entry is available.
        """
        ...


class StorageMaintenance(Protocol):
    """Prepare storage at startup and create backups during installation."""

    def initialize(self) -> None:
        """Apply pending schema migrations before the application accesses stored data."""
        ...

    def create_backup(self, destination: Path) -> None:
        """Create a consistent backup without interrupting other connections."""
        ...
