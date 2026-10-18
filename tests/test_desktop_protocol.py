"""Exercise the public desktop adapter over an actual framed Unix socket."""

import json
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from carrie_on.codex.adapter import CodexAdapter
from carrie_on.codex.client import CodexConnections, Desktop
from carrie_on.ports import CodexUnavailable
from tests.fixtures import CHAT_ID, LocalCodex


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
        self.owner_after = None
        self.settings_reads = 0
        self.malformed = None
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
                    if method == "initialize" and self.malformed is not None:
                        payload = self.malformed
                        connection.sendall(struct.pack("<I", len(payload)) + payload)
                        return
                    if method == "initialize":
                        result = {"clientId": "fixture-client"}
                    elif method == "thread-follower-read-model-settings":
                        self.settings_reads += 1
                        if self.owner_after and self.settings_reads >= self.owner_after:
                            self.dormant = False
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

    def test_missing_turn_receipt_is_uncertain_not_success(self):
        self.receipt = {"result": {}}
        self.worker.start()
        with Desktop(self.path, timeout=2) as desktop:
            with self.assertRaisesRegex(CodexUnavailable, "no accepted turn ID"):
                desktop.continue_chat("thread-a", "Finish tests", "message-a")
        self.finish()

    def test_dormant_chat_opens_once_and_waits_for_its_owner(self):
        self.dormant = True
        self.worker.start()

        def opened(*args, **kwargs):
            self.owner_after = 3

        with patch("carrie_on.codex.client.subprocess.run", side_effect=opened) as opener:
            with Desktop(self.path, timeout=2, open_if_needed=True) as desktop:
                self.assertEqual(desktop.read_settings(CHAT_ID), {"model": "model-a"})
        self.finish()
        opener.assert_called_once()
        self.assertEqual(
            opener.call_args.args[0], ["/usr/bin/open", "-g", f"codex://threads/{CHAT_ID}"]
        )
        self.assertEqual(self.settings_reads, 3)
        self.assertEqual(self.requests, [])

    def test_archived_chat_never_connects_to_desktop_or_opens_it(self):
        adapter = CodexAdapter(CodexConnections("fixture", self.path))
        with (
            patch("carrie_on.codex.client.AppServer", LocalCodex),
            patch.object(LocalCodex, "list_all_chats", return_value=[]),
            patch(
                "carrie_on.codex.client.Desktop", side_effect=AssertionError("Archived chat opened")
            ),
        ):
            self.assertIsNone(adapter.get_chat(UUID(CHAT_ID), open_if_needed=True))

    def test_invalid_encoding_retains_the_original_cause(self):
        self.malformed = b"\xff"
        self.worker.start()
        with self.assertRaises(CodexUnavailable) as caught:
            with Desktop(self.path, timeout=2):
                self.fail("Invalid encoding was accepted")
        self.assertIsInstance(caught.exception.__cause__, UnicodeDecodeError)
        self.finish()

    def test_discovery_without_request_id_is_a_recoverable_failure(self):
        self.malformed = b'{"type":"client-discovery-request"}'
        self.worker.start()
        with self.assertRaisesRegex(CodexUnavailable, "discovery request ID"):
            with Desktop(self.path, timeout=2):
                self.fail("Invalid discovery was accepted")
        self.finish()


if __name__ == "__main__":
    unittest.main()
