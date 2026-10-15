"""Observe desktop history for integration tests. The app does not import this module."""

import time

from carrie_on.codex.client import Desktop
from carrie_on.codex.models import CodexProtocolError


class DesktopObserver(Desktop):
    """Read desktop snapshots independently of the queue's SDK checks."""

    def snapshot(self, thread_id):
        """Return the selected chat's full history from a version 11 broadcast."""
        self.read_settings(thread_id)
        self._send(
            {
                "type": "broadcast",
                "sourceClientId": self.client_id,
                "version": 1,
                "method": "thread-stream-following-changed",
                "params": {"conversationId": thread_id, "hostId": "local", "following": True},
            }
        )
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._receive(deadline)
            if message.get("method") != "thread-stream-state-changed":
                continue
            params = message.get("params", {})
            change = params.get("change", {})
            if params.get("conversationId") == thread_id and change.get("type") == "snapshot":
                if message.get("version") != 11:
                    raise CodexProtocolError("Unsupported Codex snapshot version")
                return change["conversationState"]


def history_turns(snapshot):
    """Return turns from either supported desktop history representation."""
    history = snapshot.get("turnHistory") or {}
    if history.get("kind") == "canonical":
        return list(history["history"]["entitiesByKey"].values())
    return snapshot.get("turns", [])


def latest_turn(snapshot):
    """Return the accepted turn matching the desktop's latest message ID."""
    turns = [
        turn
        for turn in history_turns(snapshot)
        if turn.get("turnId")
        and turn.get("params", {}).get("clientUserMessageId")
        == snapshot["latestTurnStartMessageId"]
    ]
    if len(turns) != 1:
        raise ValueError("Desktop history has no unique accepted latest turn")
    return turns[0]
