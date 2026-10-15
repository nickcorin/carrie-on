"""Verify bounded queue dispatch with an oversized desktop history, entirely locally.

Exercise HTTP enrollment, JSON import, worker dispatch, SDK subprocess reads,
framed desktop IPC, SQLite persistence, and restart against synthetic peers.
No installed app, real conversation, or production queue is accessed.
"""

import json
from dataclasses import asdict
from uuid import UUID, uuid4

from carrie_on.legacy import LegacyEntry, LegacyImporter
from tests.fixtures import CHAT_ID
from tests.test_composed_service import ComposedService


class LargeHistoryFixture(ComposedService):
    def setUp(self):
        self.snapshot_requests = 0
        self.desktop_history = {"text": "x" * (33 * 1024 * 1024)}
        super().setUp()

    def send_history(self, connection):
        self.snapshot_requests += 1
        self.send(
            connection,
            {
                "type": "broadcast",
                "method": "thread-stream-state-changed",
                "version": 11,
                "params": {
                    "conversationId": CHAT_ID,
                    "change": {"type": "snapshot", "conversationState": self.desktop_history},
                },
            },
        )


def verify():
    reports = []
    for imported in (False, True):
        fixture = LargeHistoryFixture()
        try:
            fixture.setUp()
            if imported:
                entry = LegacyEntry(
                    id=str(uuid4()),
                    thread_id=CHAT_ID,
                    title="Fixture task",
                    account_id="account-a",
                    limit_id="codex",
                    anchor_message_id="original",
                    model="model-a",
                    prompt="Finish café tests\nKeep exact text.\n",
                    reset_windows={"primary": 100},
                    enrolled_while_busy=False,
                    state="sending",
                    attempted_at=50,
                )
                source = fixture.root / "resume-queue.json"
                source.write_text(
                    json.dumps({"version": 2, "entries": [asdict(entry)], "history": []})
                )
                LegacyImporter(fixture.store).import_file(source, "large-chat")
                fixture.state["usage"]["ordinaryUsageAllowed"] = True
                fixture.state["usage"]["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = 0
                fixture.save_state()
                fixture.app.state.worker.wakeup.set()
                entry_id, instruction = entry.id, entry.prompt
            else:
                entry_id, instruction = fixture.enroll_and_reset()
            fixture.wait_for(
                lambda: fixture.http.get(f"queue/{entry_id}").json()["state"] == "sent"
            )
            fixture.stop_server()
            fixture.start_server()
            assert fixture.store.get_entry(UUID(entry_id)).state == "sent"
            assert len(fixture.sends) == 1
            assert fixture.sends[0]["turnStart"]["request"]["input"][0]["text"] == instruction
            requests = [
                json.loads(line)
                for line in fixture.state_path.with_suffix(".requests.jsonl")
                .read_text()
                .splitlines()
            ]
            assert all(
                r["params"]["includeTurns"] is False
                for r in requests
                if r["method"] == "thread/read"
            )
            assert all(
                r["params"]["limit"] == 1 and r["params"]["itemsView"] == "notLoaded"
                for r in requests
                if r["method"] == "thread/turns/list"
            )
            largest = max(r["reply_bytes"] for r in requests)
            assert largest < 10_000
            assert fixture.snapshot_requests == 0
            reports.append(
                {
                    "imported": imported,
                    "entry_id": entry_id,
                    "sends": 1,
                    "desktop_history_bytes": len(json.dumps(fixture.desktop_history)),
                    "largest_sdk_reply_bytes": largest,
                    "restart_preserved_sent": True,
                }
            )
        finally:
            fixture.doCleanups()
    return reports


if __name__ == "__main__":
    print(json.dumps(verify(), indent=2))
