"""Delivery orchestration and a supervised worker with expiring SQLite ownership."""

import logging
import threading
import time
from collections.abc import Callable

from carrie_on.application import observe_eligibility
from carrie_on.codex.client import CodexConnections
from carrie_on.codex.models import CodexProtocolError, CodexUnavailable
from carrie_on.queue import CompletionOutcome, QueueError
from carrie_on.storage import QueueStore

logger = logging.getLogger(__name__)


def dispatch_next(
    store: QueueStore, connections: CodexConnections, *, clock: Callable[[], float] = time.time
) -> bool:
    """Attempt the next item with fresh observations and a stable client message ID.

    Temporary outages defer; invalid observations fail visibly. Unexpected bugs
    escape to supervision. SQLite completion is outside adapter error handling,
    so a persistence failure cannot be mistaken for an upstream rejection.
    """
    claimed = store.claim(now=clock())
    if claimed is None:
        return False
    item, delivery = claimed
    deadline = time.monotonic() + 100
    receipt = error_detail = None
    outcome: CompletionOutcome
    try:
        with (
            connections.app_server(deadline=deadline) as server,
            connections.desktop(open_if_needed=True, deadline=deadline) as desktop,
        ):
            decision = observe_eligibility(item, server, desktop, clock())
            if decision.outcome == "ready":
                if time.monotonic() >= deadline or not store.begin_send(delivery, clock()):
                    return True
                receipt = desktop.continue_chat(item.chat_id, delivery.instruction, item.id)
                outcome, reason = "sent", "Instruction accepted by Codex"
            else:
                outcome, reason = decision.outcome, decision.reason
    except CodexUnavailable as error:
        outcome, reason = "deferred", str(error)
        error_detail = reason
    except (CodexProtocolError, QueueError) as error:
        outcome, reason = "failed", str(error)
        error_detail = reason
    store.finish(delivery, outcome, reason, error=error_detail, receipt=receipt, now=clock())
    return True


class DispatchWorker:
    """Own a bounded dispatch thread; unexpected failure requests process restart."""

    def __init__(
        self,
        store: QueueStore,
        connections: CodexConnections,
        interval: float = 60,
        on_failure: Callable[[], None] = lambda: None,
    ):
        self.store, self.connections = store, connections
        self.interval, self.on_failure = interval, on_failure
        self.stopping, self.wakeup = threading.Event(), threading.Event()
        self.error: str | None = None
        self.thread = threading.Thread(target=self.run, name="queue-dispatch", daemon=True)

    @property
    def healthy(self) -> bool:
        return self.thread.is_alive() and self.error is None

    def run(self) -> None:
        try:
            while not self.stopping.is_set():
                self.wakeup.clear()
                dispatch_next(self.store, self.connections)
                count = self.store.summary()["count"]
                self.wakeup.wait(max(1, self.interval / max(1, count)))
        except Exception as error:
            self.error = str(error) or type(error).__name__
            logger.exception("Queue dispatch worker failed")
            self.on_failure()

    def stop(self) -> None:
        """Request shutdown and wait briefly; unfinished ownership expires on restart."""
        self.stopping.set()
        self.wakeup.set()
        self.thread.join(timeout=3)
