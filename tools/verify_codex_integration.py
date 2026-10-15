"""Check a selected real Codex chat using a temporary database.

The wait command reads the selected chat without sending. All commands write to
temporary storage. The continue and migrate commands send a test instruction.
Use them only with permission to send to an idle, disposable chat.
The tool changes saved reset timestamps to simulate a different quota window.
It does not verify an actual quota reset.
"""

import argparse
import json
import tempfile
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from carrie_on.application import QueueService
from carrie_on.codex.client import CodexConnections, Desktop
from carrie_on.codex.models import CodexProtocolError
from carrie_on.dispatch import DispatchWorker
from carrie_on.legacy import LegacyEntry, LegacyImporter
from carrie_on.queue import QueueEntry
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance


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


def verify(mode: str, chat_id: UUID, executable: str):
    connections = CodexConnections(executable)
    with tempfile.TemporaryDirectory(prefix="carrie-sdk-acceptance-") as directory:
        path = Path(directory) / "queue.sqlite3"
        SqliteStorageMaintenance(path).initialize()
        store = SqliteQueueStorage(path)
        with connections.app_server() as server, connections.desktop() as desktop:
            chat = server.get_chat(str(chat_id), desktop)
            if chat.thread_id != str(chat_id) or chat.busy != (mode == "wait") or chat.blocked:
                raise ValueError(
                    "Select an active unblocked chat for wait, or an idle unblocked disposable chat for sending"
                )
            usage = server.usage().select()
        now = datetime.now(timezone.utc)
        entry = QueueEntry.enroll(
            uuid4(), "Reply exactly: Carrie On desktop delivery verified.", chat, usage, now
        )
        entry = replace(
            entry,
            reset_windows={name: reset - 1 for name, reset in entry.reset_windows.items()},
        )
        if not entry.reset_ready(usage):
            raise ValueError("Available quota is required")
        if mode == "migrate":
            if not entry.anchor_message_id:
                raise ValueError("This chat has no message identity for the legacy import check")
            legacy = LegacyEntry(
                id=str(entry.id),
                thread_id=str(entry.chat_id),
                title=entry.title,
                account_id=entry.account_id,
                limit_id=entry.limit_id,
                anchor_message_id=entry.anchor_message_id,
                model=entry.model,
                prompt=entry.instructions,
                reset_windows=dict(entry.reset_windows),
                enrolled_while_busy=entry.enrolled_while_busy,
            )
            source = path.with_suffix(".json")
            source.write_text(
                json.dumps({"version": 2, "entries": [asdict(legacy)], "history": []})
            )
            LegacyImporter(store).import_file(source)
        else:
            store.create_entry(entry)
        if mode == "wait":
            results, _ = QueueService(store, connections).inspect()
            assert results[0].eligibility.outcome == "failed"
            assert "active" in results[0].eligibility.reason
            assert store.get_queue_summary().dispatching == 0
            return {
                "read_only": True,
                "active_chat_blocked": True,
                "synthetic_reset_baseline": True,
            }
        assert DispatchWorker(store, connections).send()
        assert store.get_entry(entry.id).state == "sent"
        for _ in range(2):
            assert not DispatchWorker(SqliteQueueStorage(path), connections).send()
        with DesktopObserver() as observer:
            turns = history_turns(observer.snapshot(str(chat_id)))
        matching = [
            turn
            for turn in turns
            if turn.get("turnId")
            and turn.get("params", {}).get("clientUserMessageId") == str(entry.id)
        ]
        assert len(matching) == 1
        return {
            "entry_id": str(entry.id),
            "matching_desktop_turns": 1,
            "restarted_workers_did_not_resend": True,
            "synthetic_reset_baseline": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("wait", "continue", "migrate"))
    parser.add_argument("chat_id", type=UUID)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    print(json.dumps(verify(args.mode, args.chat_id, args.codex), indent=2))


if __name__ == "__main__":
    main()
