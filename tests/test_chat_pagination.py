"""Exercise the native picker with deterministic, delayed HTTP responses."""

import json
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from carrie_on.codex.client import CodexConnections
from carrie_on.queue import EnqueueCommand, QueueItem
from carrie_on.server import create_application
from carrie_on.storage import QueueStore

ROOT = Path(__file__).resolve().parents[1]


def queue_contract_fixtures(directory: Path) -> Path:
    """Capture actual HTTP projections for the native decoder and action model."""

    def enqueue(
        store: QueueStore,
        title: str,
        timestamp: float,
        *,
        chat_id: str | None = None,
        instruction: str = "Original instruction",
    ) -> QueueItem:
        command = EnqueueCommand(
            id=str(uuid4()), chat_id=chat_id or str(uuid4()), instruction=instruction
        )
        item = QueueItem(
            id=command.id,
            chat_id=command.chat_id,
            title=title,
            account_id="test-account",
            limit_id="codex",
            anchor_message_id="anchor",
            model="test-model",
            instruction=instruction,
            reset_windows={"primary": 100},
            enrolled_while_busy=False,
            queued_at=timestamp,
            updated_at=timestamp,
        )
        store.enqueue(item, command)
        return item

    store = QueueStore(directory / "delivery-states.sqlite3")
    application = create_application(
        store, CodexConnections("unused"), version="test", run_worker=False
    )
    with TestClient(
        application, base_url="http://127.0.0.1:8766", headers={"X-Carrie-On": "1"}
    ) as client:
        enqueue(store, "Checking", 1)
        assert store.claim(now=4_000_000_000) is not None
        enqueue(store, "Sending", 2)
        sending = store.claim(now=4_000_000_000)
        assert sending is not None
        assert store.begin_send(sending[1], now=4_000_000_000.25)
        enqueue(store, "Expired", 3)
        expired = store.claim(now=100)
        assert expired is not None
        assert store.begin_send(expired[1], now=100.25)
        enqueue(store, "Waiting", 4)
        response = client.get("/api/v1/queue")
        response.raise_for_status()
        delivery_states = response.json()

    store = QueueStore(directory / "pagination.sqlite3")
    application = create_application(
        store, CodexConnections("unused"), version="test", run_worker=False
    )
    with TestClient(
        application, base_url="http://127.0.0.1:8766", headers={"X-Carrie-On": "1"}
    ) as client:
        original = enqueue(store, "Re-enrolled chat", 50)
        for index in range(50):
            enqueue(store, f"Other chat {index}", 51 + index)
        first = client.get("/api/v1/queue")
        first.raise_for_status()
        store.cancel(original.id, now=101)
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
