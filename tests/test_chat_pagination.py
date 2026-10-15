"""Exercise the native picker with deterministic, delayed HTTP responses."""

import json
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from carrie_on.codex.client import CodexConnections
from carrie_on.queue import QueueEntry
from carrie_on.server import create_application
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance
from tests.fixtures import instant, queue_entry

ROOT = Path(__file__).resolve().parents[1]


def queue_contract_fixtures(directory: Path) -> Path:
    """Capture actual HTTP projections for the native decoder and action model."""

    def enqueue(
        store: SqliteQueueStorage,
        title: str,
        timestamp: float,
        *,
        chat_id: UUID | None = None,
        instruction: str = "Original instruction",
    ) -> QueueEntry:
        entry = queue_entry(
            chat_id=chat_id or uuid4(),
            title=title,
            instructions=instruction,
            original_instructions=instruction,
            queued_at=instant(timestamp),
            updated_at=instant(timestamp),
        )
        store.create_entry(entry)
        return entry

    now = 100
    path = directory / "delivery-states.sqlite3"
    store = SqliteQueueStorage(path, clock=lambda: instant(now))
    application = create_application(
        store,
        CodexConnections("unused"),
        maintenance=SqliteStorageMaintenance(path),
        version="test",
        run_worker=False,
    )
    with TestClient(
        application, base_url="http://127.0.0.1:8766", headers={"X-Carrie-On": "1"}
    ) as client:
        expired = enqueue(store, "Expired", 3)
        store.create_delivery_attempt()
        store.update_entry_state(expired.id, "failed", error="Connection lost")
        now = 300
        enqueue(store, "Checking", 1)
        store.create_delivery_attempt()
        enqueue(store, "Sending", 2)
        store.create_delivery_attempt()
        enqueue(store, "Waiting", 4)
        response = client.get("/api/v1/queue")
        response.raise_for_status()
        delivery_states = response.json()

    path = directory / "pagination.sqlite3"
    store = SqliteQueueStorage(path)
    application = create_application(
        store,
        CodexConnections("unused"),
        maintenance=SqliteStorageMaintenance(path),
        version="test",
        run_worker=False,
    )
    with TestClient(
        application, base_url="http://127.0.0.1:8766", headers={"X-Carrie-On": "1"}
    ) as client:
        original = enqueue(store, "Re-enrolled chat", 50)
        for index in range(50):
            enqueue(store, f"Other chat {index}", 51 + index)
        first = client.get("/api/v1/queue")
        first.raise_for_status()
        store.update_entry_state(original.id, "cancelled")
        enqueue(
            store,
            "Re-enrolled chat",
            102,
            chat_id=original.chat_id,
            instruction="Replacement instruction",
        )
        second = client.get("/api/v1/queue", params={"cursor": first.json()["next_cursor"]})
        second.raise_for_status()
        queue_pages = [first.json(), second.json()]

    fixture = directory / "queue-contract.json"
    fixture.write_text(json.dumps({"delivery_states": delivery_states, "queue_pages": queue_pages}))
    return fixture


@unittest.skipUnless(platform.system() == "Darwin", "The chat picker requires macOS")
class NativeChatPagination(unittest.TestCase):
    def test_paging_retry_and_search_races(self):
        with tempfile.TemporaryDirectory(prefix="carrie-pagination-") as temporary:
            build = Path(temporary)
            fixtures = queue_contract_fixtures(build)
            # Keep the app's shared declarations, replacing only its startup entry point.
            declarations = (
                (ROOT / "menubar/main.swift")
                .read_text()
                .split('if CommandLine.arguments.contains("--self-test") {', 1)[0]
            )
            main = build / "main.swift"
            main.write_text(declarations + (ROOT / "tests/ChatPagination.swift").read_text())
            executable = build / "ChatPaginationTests"
            sources = [
                ROOT / "menubar" / name
                for name in (
                    "ChatPicker.swift",
                    "AppSetup.swift",
                    "AppLifecycle.swift",
                    "AppUpdate.swift",
                    "Brand.swift",
                )
            ]
            compile_result = subprocess.run(
                [
                    "/usr/bin/swiftc",
                    "-target",
                    f"{platform.machine()}-apple-macosx13.0",
                    "-module-cache-path",
                    str(build / "cache"),
                    "-o",
                    str(executable),
                    *map(str, sources),
                    str(main),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            result = subprocess.run(
                [str(executable), str(fixtures)], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Chat pagination checks passed", result.stdout)
            self.assertIn("Immutable creation and restart recovery checks passed", result.stdout)
            self.assertIn(
                "Actual HTTP delivery states and queue identity checks passed", result.stdout
            )


if __name__ == "__main__":
    unittest.main()
