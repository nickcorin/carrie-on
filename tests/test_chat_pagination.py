"""Test the native chat picker with HTTP fixtures that control response order and delays."""

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


def build_native_test(build: Path, source: Path) -> Path:
    """Compile the app declarations with a test entry point without launching the app."""
    # Use the app's declarations and replace its startup entry point for the test.
    declarations = (
        (ROOT / "menubar/main.swift")
        .read_text()
        .split('if CommandLine.arguments.contains("--self-test") {', 1)[0]
    )
    main = build / "main.swift"
    main.write_text(declarations + source.read_text())
    executable = build / f"{source.stem}Tests"
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
    if compile_result.returncode:
        raise RuntimeError(compile_result.stderr)
    return executable


def queue_contract_fixtures(directory: Path) -> Path:
    """Record API responses for native decoding and queue-action tests."""

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
            executable = build_native_test(build, ROOT / "tests/ChatPagination.swift")
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
