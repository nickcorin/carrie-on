"""Check current Codex state and send queued instructions under expiring delivery attempts."""

import logging
import threading
from collections.abc import Callable

from carrie_on.application import observe_eligibility
from carrie_on.ports import Codex, CodexUnavailable, QueueStorage
from carrie_on.queue import EntryState, QueueError

logger = logging.getLogger(__name__)


class DispatchWorker:
    """Run the dispatch thread and report any failure that stops it."""

    def __init__(
        self,
        store: QueueStorage,
        codex: Codex,
        interval: float = 60,
        on_failure: Callable[[], None] = lambda: None,
    ):
        self.store, self.codex = store, codex
        self.interval, self.on_failure = interval, on_failure
        self.stopping, self.wakeup = threading.Event(), threading.Event()
        self.error: str | None = None
        self.thread = threading.Thread(target=self.run, name="queue-dispatch", daemon=True)

    def send(self) -> bool:
        """Claim the next entry and check current Codex state before sending it.

        Leave delivery attempts unchanged so they expire at their saved deadlines.
        Failed entries can retry after expiry. Mark accepted messages as sent and
        entries whose chats have continued as stale. Keep both in stored history.
        Storage errors propagate and can stop the dispatch thread.
        """
        attempt = self.store.create_delivery_attempt()
        if attempt is None:
            return False
        entry = attempt.entry
        state: EntryState
        error = ""
        try:
            decision = observe_eligibility(entry, self.codex, open_if_needed=True)
            if decision.outcome == "ready":
                self.codex.send(entry.chat_id, entry.instructions, entry.id)
                state = "sent"
            else:
                state = decision.outcome
                error = decision.reason if state == "failed" else ""
        except (CodexUnavailable, QueueError) as failure:
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
        """Request shutdown and wait up to three seconds for the dispatch thread.

        An in-flight send may outlast this wait. Active delivery attempts keep
        their expiry times.
        """
        self.stopping.set()
        self.wakeup.set()
        self.thread.join(timeout=3)
