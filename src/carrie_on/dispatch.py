"""Fresh observation and sending, with immutable, expiring delivery attempts."""

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

from carrie_on.application import observe_eligibility
from carrie_on.codex.client import CodexConnections
from carrie_on.codex.models import CodexProtocolError, CodexUnavailable
from carrie_on.ports import QueueStorage
from carrie_on.queue import EntryState, QueueError

logger = logging.getLogger(__name__)


class DispatchWorker:
    """Send queued work and supervise the dispatch thread."""

    def __init__(
        self,
        store: QueueStorage,
        connections: CodexConnections,
        interval: float = 60,
        on_failure: Callable[[], None] = lambda: None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.store, self.connections = store, connections
        self.interval, self.on_failure, self.clock = interval, on_failure, clock
        self.stopping, self.wakeup = threading.Event(), threading.Event()
        self.error: str | None = None
        self.thread = threading.Thread(target=self.run, name="queue-dispatch", daemon=True)

    def send(self) -> bool:
        """Acquire the next entry, reconcile Codex state, then send if still eligible.

        Attempts expire without a completion write. A failed entry remains retryable;
        a confirmed send or a conversation that has advanced is terminal.
        """
        attempt = self.store.create_delivery_attempt()
        if attempt is None:
            return False
        entry = attempt.entry
        remaining = (attempt.expires_at - self.clock()).total_seconds()
        if remaining <= 0:
            return True
        deadline = time.monotonic() + min(100, remaining)
        state: EntryState
        error = ""
        try:
            with (
                self.connections.app_server(deadline=deadline) as server,
                self.connections.desktop(open_if_needed=True, deadline=deadline) as desktop,
            ):
                decision = observe_eligibility(entry, server, desktop, self.clock())
                if decision.outcome == "ready":
                    if time.monotonic() >= deadline:
                        raise CodexUnavailable("Delivery attempt expired before sending")
                    desktop.continue_chat(str(entry.chat_id), entry.instructions, str(entry.id))
                    state = "sent"
                else:
                    state = decision.outcome
                    error = decision.reason if state == "failed" else ""
        except (CodexUnavailable, CodexProtocolError, QueueError) as failure:
            state, error = "failed", str(failure) or type(failure).__name__
        self.store.update_entry_state(entry.id, state, error=error)
        return True

    @property
    def healthy(self) -> bool:
        return self.thread.is_alive() and self.error is None

    def run(self) -> None:
        try:
            while not self.stopping.is_set():
                self.wakeup.clear()
                self.send()
                count = self.store.get_queue_summary().count
                self.wakeup.wait(max(1, self.interval / max(1, count)))
        except Exception as error:
            self.error = str(error) or type(error).__name__
            logger.exception("Queue dispatch worker failed")
            self.on_failure()

    def stop(self) -> None:
        """Request shutdown; unfinished attempts expire on restart."""
        self.stopping.set()
        self.wakeup.set()
        self.thread.join(timeout=3)
