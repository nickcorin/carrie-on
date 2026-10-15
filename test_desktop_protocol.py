"""Exercise the public desktop adapter over an actual framed Unix socket."""

import json
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_bridge import BridgeError, DesktopClient, OwnerUnavailable
from verify_desktop import DesktopObserver


def snapshot():
    return {
        "id": "thread-a", "title": "Finish feature", "latestTurnStartMessageId": "original",
        "latestModel": "model-a", "resumeState": "resumed", "requests": [],
        "threadRuntimeStatus": {"type": "idle"},
        "turnHistory": {"kind": "canonical", "history": {"entitiesByKey": {
            "latest": {"turnId": "turn-original", "status": "failed",
                       "error": {"codexErrorInfo": "usageLimitExceeded"},
                       "params": {"clientUserMessageId": "original"}},
        }}},
    }


class DesktopProtocol(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ipc.sock"
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.path))
        self.server.listen(1)
        self.server.settimeout(2)
        self.addCleanup(self.server.close)
        self.requests = []
        self.errors = []
        self.snapshot_version = 11
        self.snapshot_data = snapshot()
        self.snapshot_requests = 0
        self.snapshot_bytes = 0
        self.snapshot_frame_length = None
        self.allow_snapshot_disconnect = False
        self.dormant = False
        self.owner_error = "no-client-found"
        self.settings_reply_method = "thread-follower-read-model-settings"
        self.receipt = {"result": {"turn": {"id": "accepted-turn"}}}
        self.worker = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        try:
            connection, _ = self.server.accept()
            with connection:
                connection.settimeout(2)

                def read(size):
                    data = bytearray()
                    while len(data) < size:
                        chunk = connection.recv(size - len(data))
                        if not chunk:
                            raise EOFError()
                        data.extend(chunk)
                    return data

                def send(message):
                    payload = json.dumps(message).encode()
                    length = len(payload)
                    if message.get("method") == "thread-stream-state-changed":
                        self.snapshot_bytes = length
                        if self.snapshot_frame_length is not None:
                            length = self.snapshot_frame_length
                    frame = struct.pack("<I", length) + payload
                    # Both headers and bodies may be fragmented in a real stream.
                    for chunk in (frame[:1], frame[1:3], frame[3:17], frame[17:]):
                        connection.sendall(chunk)

                while True:
                    message = json.loads(read(struct.unpack("<I", read(4))[0]))
                    method = message["method"]
                    if method == "thread-stream-following-changed":
                        self.snapshot_requests += 1
                        send({"type": "broadcast", "method": "thread-stream-state-changed",
                              "version": self.snapshot_version,
                              "params": {"conversationId": "thread-a", "change": {
                                  "type": "snapshot", "conversationState": self.snapshot_data}}})
                        continue
                    if method == "initialize":
                        result = {"clientId": "fixture-client"}
                    elif method == "thread-follower-read-model-settings":
                        if self.dormant:
                            send({"type": "response", "requestId": message["requestId"],
                                  "method": method, "resultType": "error", "error": self.owner_error})
                            continue
                        result = {"settings": {"model": "model-a", "resumeState": "resumed"}}
                        method = self.settings_reply_method
                    elif method == "thread-follower-start-turn":
                        self.requests.append(message)
                        result = self.receipt
                    else:
                        raise AssertionError(f"Unexpected method: {method}")
                    send({"type": "response", "requestId": message["requestId"],
                          "method": method, "resultType": "success", "result": result})
        except EOFError:
            pass
        except (BrokenPipeError, ConnectionResetError) as error:
            if not self.allow_snapshot_disconnect:
                self.errors.append(error)
        except BaseException as error:
            self.errors.append(error)

    def finish(self):
        self.worker.join(3)
        self.assertFalse(self.worker.is_alive())
        if self.errors:
            raise self.errors[0]

    def test_live_snapshot_and_turn_dispatch_preserve_desktop_settings(self):
        self.worker.start()
        with DesktopObserver(self.path, timeout=2) as desktop:
            self.assertEqual(desktop.snapshot("thread-a")["title"], "Finish feature")
            self.assertEqual(desktop.continue_chat("thread-a", "Finish tests", "message-a"),
                             "accepted-turn")
        self.finish()
        self.assertEqual(self.requests[0]["params"], {
            "conversationId": "thread-a",
            "turnStart": {"request": {"threadId": "thread-a", "clientUserMessageId": "message-a",
                                      "input": [{"type": "text", "text": "Finish tests", "text_elements": []}]},
                          "context": {"inheritThreadSettings": True}},
        })
        self.assertEqual(self.requests[0]["version"], 3)

    def test_large_diagnostic_snapshot_is_still_rejected(self):
        self.allow_snapshot_disconnect = True
        self.snapshot_data["turnHistory"]["history"]["entitiesByKey"]["earlier"] = {
            "turnId": "older-turn", "status": "completed",
            "params": {"clientUserMessageId": "older-message",
                       "input": [{"type": "text", "text": "x" * (33 * 1024 * 1024)}]},
        }
        self.worker.start()
        with DesktopObserver(self.path, timeout=5) as desktop:
            with self.assertRaisesRegex(BridgeError, "Invalid or oversized Codex desktop frame"):
                desktop.snapshot("thread-a")
        self.finish()
        self.assertGreater(self.snapshot_bytes, 32 * 1024 * 1024)

    def test_oversized_frame_is_rejected_before_body_read(self):
        self.snapshot_frame_length = 32 * 1024 * 1024 + 1
        self.worker.start()
        with DesktopObserver(self.path, timeout=2) as desktop:
            with self.assertRaisesRegex(BridgeError, "Invalid or oversized Codex desktop frame"):
                desktop.snapshot("thread-a")
        self.finish()

    def test_unknown_snapshot_protocol_fails_closed(self):
        self.snapshot_version = 999
        self.worker.start()
        with DesktopObserver(self.path, timeout=2) as desktop:
            with self.assertRaisesRegex(BridgeError, "Unsupported Codex snapshot version"):
                desktop.snapshot("thread-a")
        self.finish()

    def test_settings_and_dispatch_do_not_require_a_history_subscription(self):
        self.worker.start()
        with DesktopClient(self.path, timeout=2) as desktop:
            self.assertEqual(desktop.read_settings("thread-a"), {"model": "model-a", "resumeState": "resumed"})
            self.assertEqual(desktop.continue_chat("thread-a", "Finish tests", "message-a"),
                             "accepted-turn")
        self.finish()
        self.assertEqual(self.requests[0]["params"]["turnStart"]["context"], {"inheritThreadSettings": True})

    def test_missing_turn_receipt_is_uncertain_not_success(self):
        self.receipt = {"result": {}}
        self.worker.start()
        with DesktopClient(self.path, timeout=2) as desktop:
            with self.assertRaisesRegex(BridgeError, "no accepted turn ID"):
                desktop.continue_chat("thread-a", "Finish tests", "message-a")
        self.finish()

    def test_settings_read_does_not_open_a_dormant_chat(self):
        self.dormant = True
        self.worker.start()
        with patch("codex_bridge.subprocess.run", side_effect=AssertionError("Settings must not open a chat")):
            with DesktopClient(self.path, timeout=2) as desktop:
                with self.assertRaises(OwnerUnavailable):
                    desktop.read_settings("thread-a")
        self.finish()

    def test_open_chat_returns_settings_after_owner_appears(self):
        self.dormant = True
        self.worker.start()
        def open_chat(command, **kwargs):
            self.assertEqual(command, ["/usr/bin/open", "-g", "codex://threads/aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"])
            self.dormant = False
        with patch("codex_bridge.subprocess.run", side_effect=open_chat):
            with DesktopClient(self.path, timeout=2) as desktop:
                self.assertEqual(desktop.open_chat("aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"),
                                 {"model": "model-a", "resumeState": "resumed"})
        self.finish()

    def test_owner_error_with_description_is_recognized(self):
        self.dormant = True
        self.owner_error = "no-client-found: thread stream owner is unavailable"
        self.worker.start()
        with DesktopClient(self.path, timeout=2) as desktop:
            with self.assertRaisesRegex(OwnerUnavailable, "thread stream owner is unavailable"):
                desktop.read_settings("thread-a")
        self.finish()

    def test_open_chat_propagates_protocol_error_without_retry(self):
        self.settings_reply_method = "wrong-method"
        self.worker.start()
        with patch("codex_bridge.subprocess.run"), \
                patch("codex_bridge.time.sleep", side_effect=AssertionError("Must not retry a protocol error")):
            with DesktopClient(self.path, timeout=2) as desktop:
                with self.assertRaisesRegex(BridgeError, "response method mismatch"):
                    desktop.open_chat("aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")
        self.finish()


if __name__ == "__main__":
    unittest.main()
