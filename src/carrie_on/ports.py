"""Consumer-owned persistence contracts; no database types cross this boundary."""

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol
from uuid import UUID

from carrie_on.queue import DeliveryAttempt, EntryState, QueueEntry, QueueSummary


class StorageUnavailable(Exception):
    """Persistence could not complete; a lost reply does not establish rollback."""


class QueueStorage(Protocol):
    """Atomic queue operations. Mutations return persisted entries or raise QueueError."""

    def get_entry(self, entry_id: UUID) -> QueueEntry | None:
        """Return the entry, or None when its identity is unknown."""
        ...

    def list_entries(
        self,
        *,
        states: tuple[EntryState, ...] | None = ("queued", "failed"),
        chat_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[QueueEntry], str | None]:
        """Read 1–100 entries in (queued_at, id) order; None includes every state.

        Cursors are opaque and bound to the filters. Separate pages are live
        observations, not a snapshot across calls.
        """
        ...

    def get_queue_summary(self) -> QueueSummary:
        """Count entry states and active dispatches in a consistent read."""
        ...

    def create_entry(self, entry: QueueEntry) -> tuple[QueueEntry, bool]:
        """Create pending work or replay its original UUID/input; bool means created.

        Different original input conflicts. A conversation can have at most one
        pending entry, including failed work. Creation performs no Codex I/O.
        """
        ...

    def update_instructions(
        self, entry_id: UUID, instructions: str, *, revision: int
    ) -> QueueEntry:
        """Edit idle pending work at its current revision; conflict changes nothing."""
        ...

    def update_entry_state(
        self, entry_id: UUID, state: EntryState, *, error: str = ""
    ) -> QueueEntry:
        """Change state/error atomically, leaving delivery attempts unchanged."""
        ...

    def create_delivery_attempt(self) -> DeliveryAttempt | None:
        """Acquire the next pending entry without an active attempt, or return None."""
        ...

    def create_entries(self, entries: Sequence[QueueEntry], *, import_id: UUID) -> bool:
        """Insert a batch and marker together; completed imports return False."""
        ...


class StorageMaintenance(Protocol):
    """Persistence lifecycle capabilities for startup and installation."""

    def initialize(self) -> None:
        """Apply pending schema migrations before application operations."""
        ...

    def create_backup(self, destination: Path) -> None:
        """Create a consistent backup without interrupting other connections."""
        ...
