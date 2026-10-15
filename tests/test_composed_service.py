"""HTTP → lifespan worker → real Codex transports → SQLite, with synthetic peers."""

import json
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from carrie_on.codex.client import CodexConnections
from carrie_on.server import create_application
from carrie_on.storage import QueueStore
from tests.fixtures import CHAT_ID, snapshot, usage


class ComposedService(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state_path = self.root / "codex.json"
        self.state = {
            "usage": usage(100, int(time.time()) + 120),
            "summary": {"id": CHAT_ID, "name": "Fixture task", "cwd": "/fixture", "updatedAt": 100},
        }
        self.save_state()
        executable = self.root / "codex"
        executable.write_text(
            f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
            f"from tests.codex_peer import serve\nserve({str(self.state_path)!r})\n"
        )
        executable.chmod(0o700)
        self.socket_path = self.root / "ipc.sock"
        self.peer = socket.socket(socket.AF_UNIX)
        self.peer.bind(str(self.socket_path))
        self.peer.listen()
        self.peer.settimeout(0.1)
        self.stop_peer = threading.Event()
        self.peer_errors = []
        self.sends = []
        self.accepted_id = None
        self.lose_receipt = False
        self.peer_thread = threading.Thread(target=self.serve_desktop, daemon=True)
        self.peer_thread.start()
        self.addCleanup(self.close_peer)
        self.store = QueueStore(self.root / "queue.sqlite3")
        self.connections = CodexConnections(str(executable), self.socket_path)
        self.server = None
        self.start_server()
        self.addCleanup(self.stop_server)

    def save_state(self):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state))
        temporary.replace(self.state_path)

    def start_server(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        self.app = create_application(self.store, self.connections, port=port, interval=1)
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="critical"))
        self.server_thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        self.server_thread.start()
        self.listener = listener
        self.wait_for(lambda: self.server.started)
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{port}/api/v1/", headers={"X-Carrie-On": "1"}
        )

    def stop_server(self):
        if self.server:
            self.http.close()
            self.server.should_exit = True
            self.server_thread.join(5)
            self.listener.close()
            self.assertFalse(self.server_thread.is_alive())
            self.server = None

    def close_peer(self):
        self.stop_peer.set()
        self.peer_thread.join(3)
        self.peer.close()
        self.assertFalse(self.peer_thread.is_alive())
        self.assertEqual(self.peer_errors, [])

    @staticmethod
    def read_bytes(connection, size):
        result = bytearray()
        while len(result) < size:
            data = connection.recv(size - len(result))
            if not data:
                raise EOFError
            result.extend(data)
        return result

    @staticmethod
    def send(connection, message):
        data = json.dumps(message).encode()
        connection.sendall(struct.pack("<I", len(data)) + data)

    def serve_desktop(self):
        while not self.stop_peer.is_set():
            try:
                connection, _ = self.peer.accept()
            except socket.timeout:
                continue
            try:
                with connection:
                    connection.settimeout(2)
                    self.handle_desktop(connection)
            except EOFError:
                pass
            except Exception as error:
                self.peer_errors.append(error)

    def handle_desktop(self, connection):
        while True:
            size = struct.unpack("<I", self.read_bytes(connection, 4))[0]
            message = json.loads(self.read_bytes(connection, size))
            method = message["method"]
            if method == "thread-stream-following-changed":
                chat = (
                    snapshot("inProgress", self.accepted_id, "active")
                    if self.accepted_id
                    else snapshot()
                )
                chat["id"] = CHAT_ID
                self.send(
                    connection,
                    {
                        "type": "broadcast",
                        "method": "thread-stream-state-changed",
                        "version": 11,
                        "params": {
                            "conversationId": CHAT_ID,
                            "change": {"type": "snapshot", "conversationState": chat},
                        },
                    },
                )
                continue
            if method == "initialize":
                result = {"clientId": "fixture-client"}
            elif method == "thread-follower-read-model-settings":
                result = {"settings": {"model": "model-a"}}
            elif method == "thread-follower-start-turn":
                self.sends.append(message["params"])
                self.accepted_id = message["params"]["turnStart"]["request"]["clientUserMessageId"]
                if self.lose_receipt:
                    return
                result = {"result": {"turn": {"id": "accepted-fixture-turn"}}}
            else:
                raise AssertionError(method)
            self.send(
                connection,
                {
                    "type": "response",
                    "requestId": message["requestId"],
                    "method": method,
                    "resultType": "success",
                    "result": result,
                },
            )

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Composed service did not reach the expected state")

    def enroll_and_reset(self):
        item_id = str(uuid4())
        instruction = "Finish café tests\nKeep exact text.\n"
        response = self.http.post(
            "queue", json={"id": item_id, "chat_id": CHAT_ID, "instruction": instruction}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.wait_for(lambda: self.http.get(f"queue/{item_id}").json()["deferrals"] > 0)
        self.assertEqual(self.sends, [])
        self.state["usage"] = usage(0, int(time.time()) + 3600)
        self.save_state()
        self.app.state.worker.wakeup.set()
        return item_id, instruction

    def test_http_enrollment_worker_send_and_durable_receipt(self):
        item_id, instruction = self.enroll_and_reset()
        self.wait_for(lambda: self.http.get(f"queue/{item_id}").json()["state"] == "sent")
        self.assertEqual(len(self.sends), 1)
        request = self.sends[0]["turnStart"]["request"]
        self.assertEqual(request["clientUserMessageId"], item_id)
        self.assertEqual(request["input"][0]["text"], instruction)
        self.assertEqual(self.sends[0]["turnStart"]["context"], {"inheritThreadSettings": True})
        attempts = self.http.get(f"queue/{item_id}/deliveries").json()["items"]
        self.assertEqual(attempts[-1]["receipt"], "accepted-fixture-turn")
        self.assertEqual(attempts[-1]["outcome"], "sent")
        self.stop_server()
        self.start_server()
        self.assertEqual(self.http.get(f"queue/{item_id}").json()["state"], "sent")
        self.assertEqual(self.http.get("queue").json()["items"], [])
        self.assertEqual(len(self.sends), 1)

    def test_lost_receipt_reconciles_after_service_restart_without_another_send(self):
        self.lose_receipt = True
        item_id, _ = self.enroll_and_reset()
        self.wait_for(lambda: bool(self.sends))
        self.wait_for(lambda: self.http.get(f"queue/{item_id}").json()["delivery_id"] is None)
        self.stop_server()
        self.start_server()
        self.wait_for(lambda: self.http.get(f"queue/{item_id}").json()["state"] == "sent")
        self.assertEqual(len(self.sends), 1)
        attempts = self.http.get(f"queue/{item_id}/deliveries").json()["items"]
        self.assertEqual(attempts[-1]["reason"], "Confirmed the previously accepted instruction")
        self.assertTrue(any(attempt["outcome"] == "deferred" for attempt in attempts))

    def test_legacy_usage_shape_normalizes_through_real_adapter_and_http(self):
        bucket = self.state["usage"].pop("rateLimitsByLimitId")["codex"]
        bucket["primary"]["windowDurationMins"] = 300
        self.state["usage"]["rateLimits"] = {"limitId": "codex", **bucket}
        self.save_state()
        usage_response = self.http.get("usage")
        self.assertEqual(usage_response.status_code, 200, usage_response.text)
        self.assertEqual(
            usage_response.json()["rateLimitsByLimitId"]["codex"]["primary"]["windowDurationMins"],
            300,
        )
        status = self.http.get("status").json()
        self.assertEqual(status["usage"]["windows"][0]["window_minutes"], 300)
        self.assertIsNone(status["usage_error"])
