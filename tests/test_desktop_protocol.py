"""Exercise the public desktop adapter over an actual framed Unix socket."""

import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from carrie_on.codex.client import Desktop
from carrie_on.codex.models import CodexUnavailable


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
        self.dormant = False
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
                    frame = struct.pack("<I", len(payload)) + payload
                    # Both headers and bodies may be fragmented in a real stream.
                    for chunk in (frame[:1], frame[1:3], frame[3:17], frame[17:]):
                        connection.sendall(chunk)

                while True:
                    message = json.loads(read(struct.unpack("<I", read(4))[0]))
                    method = message["method"]
                    if method == "initialize":
                        result = {"clientId": "fixture-client"}
                    elif method == "thread-follower-read-model-settings":
                        if self.dormant:
                            send(
                                {
                                    "type": "response",
                                    "requestId": message["requestId"],
                                    "method": method,
                                    "resultType": "error",
                                    "error": "no-client-found",
                                }
                            )
                            continue
                        result = {"settings": {"model": "model-a"}}
                    elif method == "thread-follower-start-turn":
                        self.requests.append(message)
                        result = self.receipt
                    else:
                        raise AssertionError(f"Unexpected method: {method}")
                    send(
                        {
                            "type": "response",
                            "requestId": message["requestId"],
                            "method": method,
                            "resultType": "success",
                            "result": result,
                        }
                    )
        except EOFError:
            pass
        except BaseException as error:
            self.errors.append(error)

    def finish(self):
        self.worker.join(3)
        self.assertFalse(self.worker.is_alive())
        if self.errors:
            raise self.errors[0]

    def test_compact_settings_and_turn_dispatch_preserve_desktop_settings(self):
        self.worker.start()
        with Desktop(self.path, timeout=2) as desktop:
            self.assertEqual(desktop.read_settings("thread-a")["model"], "model-a")
            self.assertEqual(
                desktop.continue_chat("thread-a", "Finish tests", "message-a"), "accepted-turn"
            )
        self.finish()
        self.assertEqual(
            self.requests[0]["params"],
            {
                "conversationId": "thread-a",
                "turnStart": {
                    "request": {
                        "threadId": "thread-a",
                        "clientUserMessageId": "message-a",
                        "input": [{"type": "text", "text": "Finish tests", "text_elements": []}],
                    },
                    "context": {"inheritThreadSettings": True},
                },
            },
        )
        self.assertEqual(self.requests[0]["version"], 3)

    def test_expired_attempt_cannot_send_a_message(self):
        self.worker.start()
        with Desktop(self.path, timeout=2) as desktop:
            desktop.read_settings("thread-a")
            desktop.deadline = time.monotonic() - 1
            with self.assertRaisesRegex(CodexUnavailable, "deadline expired"):
                desktop.continue_chat("thread-a", "Do not send", "expired-attempt")
        self.finish()
        self.assertEqual(self.requests, [])

    def test_missing_turn_receipt_is_uncertain_not_success(self):
        self.receipt = {"result": {}}
        self.worker.start()
        with Desktop(self.path, timeout=2) as desktop:
            with self.assertRaisesRegex(CodexUnavailable, "no accepted turn ID"):
                desktop.continue_chat("thread-a", "Finish tests", "message-a")
        self.finish()

    def test_dormant_chat_is_validated_before_opening(self):
        self.dormant = True
        self.worker.start()

        def reject_archived():
            raise ValueError("Chat is archived")

        with patch(
            "carrie_on.codex.client.subprocess.run",
            side_effect=AssertionError("Must not open an archived chat"),
        ):
            with Desktop(self.path, timeout=2, open_if_needed=True) as desktop:
                with self.assertRaisesRegex(ValueError, "Chat is archived"):
                    desktop.read_settings(
                        "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", before_open=reject_archived
                    )
        self.finish()


if __name__ == "__main__":
    unittest.main()
