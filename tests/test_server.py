"""Exercise startup, explicit shutdown, and worker failure through server.main."""

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

import httpx

from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance
from tests.fixtures import instant, queue_entry


class ServerLifetime(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "queue.sqlite3"
        SqliteStorageMaintenance(self.path).initialize()
        self.store = SqliteQueueStorage(self.path)
        self.first = queue_entry()
        self.second = queue_entry(chat_id=uuid4(), queued_at=instant(51))
        self.store.create_entry(self.first)
        self.store.create_entry(self.second)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}/api/v1/",
            headers={"X-Carrie-On": "1"},
            timeout=1,
            trust_env=False,
        )
        self.addCleanup(self.http.close)

    def start(self, *, fail_worker=False):
        args = [
            "--state",
            str(self.path),
            "--port",
            str(self.port),
            "--codex",
            str(self.root / "missing-codex"),
            "serve",
        ]
        script = """
import sys
from unittest.mock import patch
from carrie_on import server
from carrie_on.dispatch import DispatchWorker

def fail(self):
    raise RuntimeError("Controlled worker failure")

with patch.object(DispatchWorker, "send", fail):
    raise SystemExit(server.main())
"""
        command = (
            [sys.executable, "-c", script]
            if fail_worker
            else [sys.executable, "-m", "carrie_on.server"]
        )
        process = subprocess.Popen(
            command + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)

        self.addCleanup(cleanup)
        return process

    def test_missing_executable_keeps_queue_available_and_api_shutdown_exits_zero(self):
        process = self.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll())
            try:
                if self.http.get("health").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            self.fail("Server did not become ready")
        status = self.http.get("status").json()
        self.assertIsNone(status["usage"])
        self.assertIn("missing-codex", status["usage_error"])
        self.assertEqual(status["queue"]["count"], 2)
        self.assertEqual(len(self.http.get("queue").json()["items"]), 2)
        edited = self.http.patch(
            f"queue/{self.second.id}", json={"instruction": "Edited offline", "revision": 1}
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(self.http.post(f"queue/{self.second.id}/cancel", json={}).status_code, 200)
        self.assertEqual(self.http.post("service/shutdown", json={}).status_code, 202)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        saved = SqliteQueueStorage(self.path).get_entry(self.second.id)
        self.assertEqual((saved.state, saved.instructions), ("cancelled", "Edited offline"))
        repeated = subprocess.run(
            [
                sys.executable,
                "-m",
                "carrie_on",
                "--server",
                f"http://127.0.0.1:{self.port}",
                "--json",
                "service",
                "stop",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout), {"stopping": False})

    def test_worker_failure_terminates_server_and_preserves_queue(self):
        process = self.start(fail_worker=True)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 1, stdout + stderr)
        self.assertIn("Controlled worker failure", stderr)
        self.assertEqual(SqliteQueueStorage(self.path).get_entry(self.first.id), self.first)
