"""Model-free Codex reads and desktop-owned continuation.

The app-server subprocess is restricted to read methods. Desktop dispatch uses
the versioned, same-user IPC interface; no authentication files are inspected.
Both clients own their connection and must be used as context managers.
"""

import json
import os
import select
import socket
import stat
import struct
import subprocess
import time
import uuid
from pathlib import Path

from app_runtime import VERSION


class BridgeError(RuntimeError):
    """Unavailable or incompatible Codex connection; a send may be uncertain."""


class AppServer:
    """Read account limits and stored chat summaries without running inference."""

    def __init__(self, executable="codex", timeout=30):
        self.executable = executable
        self.timeout = timeout
        self.buffer = b""
        self.process = None

    def __enter__(self):
        self.process = subprocess.Popen(
            [self.executable, "app-server"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            self.request("initialize", {
                "clientInfo": {"name": "carrie_on", "version": VERSION},
            })
            self._send({"method": "initialized"})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()

    def _send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, method, params=None):
        """Return a read RPC result, raising on timeout or RPC failure."""
        if method not in {"initialize", "account/rateLimits/read", "thread/list", "thread/read"}:
            raise ValueError("The monitoring app-server is read-only")
        request_id = str(uuid.uuid4())
        self._send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                remaining = max(0, deadline - time.monotonic())
                if not select.select([self.process.stdout], [], [], remaining)[0]:
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise BridgeError("Codex app-server closed; check Codex login and configuration")
                self.buffer += chunk
                if len(self.buffer) > 32 * 1024 * 1024:
                    raise BridgeError("Codex response exceeded 32 MiB")
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            message = json.loads(line)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise BridgeError(f"{method}: {message['error'].get('message', 'RPC failed')}")
            return message["result"]
        raise BridgeError(f"{method} timed out")

    def usage(self):
        """Fetch fresh quota metadata; this does not start a model turn."""
        return self.request("account/rateLimits/read")

    def threads(self, search=None):
        """Return up to 100 recent, unarchived local chat summaries."""
        return self.thread_page(search, limit=100)["data"]

    def thread_page(self, search=None, *, cursor=None, limit=25):
        """Read recent unarchived summaries and an opaque nextCursor (null at end).

        Continue with the returned cursor and the same search and ordering.
        Concurrent activity can move chats between pages; this is not a snapshot.
        """
        params = {"limit": limit, "archived": False, "sourceKinds": [],
                  "sortKey": "updated_at", "sortDirection": "desc"}
        if search:
            params["searchTerm"] = search
        if cursor:
            params["cursor"] = cursor
        return self.request("thread/list", params)


class Desktop:
    """Coordinate with the running desktop app over its private IPC socket.

    The protocol versions below match the inspected desktop build. Unknown
    snapshot versions fail closed. Never claim ownership of another chat.
    """

    def __init__(self, path=None, timeout=15, open_if_needed=False):
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.path = Path(path) if path else home / "ipc" / "ipc.sock"
        self.timeout = timeout
        self.open_if_needed = open_if_needed
        self.client_id = "carrie_on"
        self.socket = None
        self.snapshots = {}

    def __enter__(self):
        info = self.path.lstat()
        parent = self.path.parent.stat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or parent.st_uid != os.getuid() or parent.st_mode & 0o022):
            raise BridgeError("Codex IPC socket must be in a private, user-owned directory")
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(self.timeout)
        try:
            self.socket.connect(str(self.path))
            reply = self.request("initialize", {"clientType": "carrie_on"}, version=0)
            self.client_id = reply["clientId"]
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.socket is not None:
            self.socket.close()

    def _send(self, message):
        payload = json.dumps(message).encode()
        self.socket.sendall(struct.pack("<I", len(payload)) + payload)

    def _read_bytes(self, count, deadline):
        chunks = bytearray()
        while len(chunks) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError("Codex desktop response timed out")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(count - len(chunks))
            if not chunk:
                raise BridgeError("Codex desktop disconnected")
            chunks.extend(chunk)
        return chunks

    def _receive(self, deadline):
        length = struct.unpack("<I", self._read_bytes(4, deadline))[0]
        if not 0 < length <= 32 * 1024 * 1024:
            raise BridgeError("Invalid or oversized Codex desktop frame")
        message = json.loads(self._read_bytes(length, deadline))
        if message.get("type") == "client-discovery-request":
            self._send({"type": "client-discovery-response", "requestId": message["requestId"],
                        "response": {"canHandle": False}})
        if message.get("method") == "thread-stream-state-changed":
            params = message.get("params", {})
            change = params.get("change", {})
            if change.get("type") == "snapshot":
                if message.get("version") != 11:
                    raise BridgeError("Unsupported Codex snapshot version; update Carrie On")
                self.snapshots[params["conversationId"]] = change["conversationState"]
        return message

    def request(self, method, params, version=2, host="local"):
        """Send a versioned IPC request. Timeouts do not establish non-delivery."""
        request_id = str(uuid.uuid4())
        message = {"type": "request", "requestId": request_id,
                   "sourceClientId": self.client_id, "version": version,
                   "method": method, "params": params}
        if method != "initialize":
            message["hostId"] = host
        self._send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            reply = self._receive(deadline)
            if reply.get("type") != "response" or reply.get("requestId") != request_id:
                continue
            if reply.get("resultType") != "success":
                raise BridgeError(f"{method}: {reply.get('error', 'request failed')}")
            if reply.get("method") != method:
                raise BridgeError("Codex desktop response method mismatch")
            return reply["result"]

    def snapshot(self, thread_id, *, before_open=None):
        """Get live state, optionally opening an explicitly selected dormant chat.

        Opening uses the app's ordinary deep link and can change the visible tab.
        It does not submit a prompt. Callers must exclude archived chats first,
        or supply before_open to validate only when opening is necessary.
        """
        self.snapshots.pop(thread_id, None)
        try:
            self.request("thread-follower-read-model-settings", {"conversationId": thread_id})
        except BridgeError as error:
            if not self.open_if_needed or "no-client-found" not in str(error):
                raise
            uuid.UUID(thread_id)
            if before_open is not None:
                before_open()
            subprocess.run(["/usr/bin/open", "-g", f"codex://threads/{thread_id}"],
                           check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 20
            while True:
                try:
                    self.request("thread-follower-read-model-settings", {"conversationId": thread_id})
                    break
                except BridgeError:
                    if time.monotonic() >= deadline:
                        raise BridgeError("Open the queued chat in Codex; its owner is unavailable") from error
                    time.sleep(0.5)
        self._send({"type": "broadcast", "sourceClientId": self.client_id, "version": 1,
                    "method": "thread-stream-following-changed",
                    "params": {"conversationId": thread_id, "hostId": "local", "following": True}})
        deadline = time.monotonic() + self.timeout
        while thread_id not in self.snapshots:
            self._receive(deadline)
        return self.snapshots.pop(thread_id)

    def continue_chat(self, thread_id, prompt, message_id):
        """Ask the owning app to start a turn with its existing chat settings.

        The caller must durably record message_id before calling. Errors may
        occur after delivery; reconcile live state before any further attempt.
        """
        receipt = self.request("thread-follower-start-turn", {
            "conversationId": thread_id,
            "turnStart": {
                "request": {"threadId": thread_id, "clientUserMessageId": message_id,
                            "input": [{"type": "text", "text": prompt, "text_elements": []}]},
                "context": {"inheritThreadSettings": True},
            },
        }, version=3)
        turn_id = receipt.get("result", {}).get("turn", {}).get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise BridgeError("Desktop returned no accepted turn ID; delivery is uncertain")
        return {"turn_id": turn_id}
