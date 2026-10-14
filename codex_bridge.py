"""Model-free Codex reads and desktop-owned continuation.

The app-server subprocess is restricted to read methods. Desktop dispatch uses
the versioned, same-user IPC interface; no authentication files are inspected.
Both clients own their connection and must be used as context managers.
"""

import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.errors import CodexError
from openai_codex.generated.v2_all import (
    GetAccountRateLimitsResponse, ThreadReadResponse, ThreadListResponse,
    ThreadTurnsListResponse, ThreadGoalGetResponse,
)

from app_runtime import VERSION


class BridgeError(RuntimeError):
    """Unavailable or incompatible Codex connection; a send may be uncertain."""


MAX_RESPONSE_BYTES = 1024 * 1024


class BoundedCodexClient(CodexClient):
    """SDK transport with a byte circuit breaker and per-request deadline.

    The pinned SDK owns JSON-RPC routing and process lifecycle. These overrides
    bound reads before decoding and terminate only this client's subprocess on
    timeout. Callers must serialize requests on this client.
    """

    def __init__(self, executable, timeout):
        super().__init__(CodexConfig(codex_bin=shutil.which(executable) or executable,
                                    client_name="carrie_on", client_version=VERSION))
        self.timeout = timeout

    def _read_message(self):
        line = self._proc.stdout.buffer.readline(MAX_RESPONSE_BYTES + 1)
        if len(line) > MAX_RESPONSE_BYTES:
            raise BridgeError("Codex response exceeded 1 MiB; connection closed")
        if not line:
            raise BridgeError("Codex app-server disconnected or timed out")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise BridgeError("Invalid Codex response")
        return message

    def close(self):
        process = self._proc
        super().close()
        if process is not None:
            process.wait(timeout=2)
            process.stdout.close()
            process.stderr.close()

    def _request_raw(self, method, params=None):
        process = self._proc
        expired = threading.Event()

        def expire():
            expired.set()
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

        timer = threading.Timer(self.timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            return super()._request_raw(method, params)
        finally:
            timer.cancel()
            timer.join()
            if expired.is_set():
                raise BridgeError(f"{method} timed out")

    def _handle_server_request(self, message):
        raise BridgeError("The monitoring app-server cannot answer approvals or run tools")


class AppServer:
    """Read account limits and stored chat summaries without running inference."""

    def __init__(self, executable="codex", timeout=30):
        self.executable = executable
        self.timeout = timeout
        self.client = None

    def __enter__(self):
        self.client = BoundedCodexClient(self.executable, self.timeout)
        try:
            self.client.start()
            self.client.initialize()
        except BaseException as error:
            self.__exit__(None, None, None)
            if isinstance(error, CodexError):
                raise BridgeError(f"Codex SDK initialization failed: {error}") from error
            raise
        return self

    def __exit__(self, *_):
        if self.client is not None:
            self.client.close()

    def request(self, method, params=None):
        """Return a read RPC result, raising on timeout or RPC failure."""
        responses = {
            "account/rateLimits/read": GetAccountRateLimitsResponse,
            "thread/list": ThreadListResponse,
            "thread/read": ThreadReadResponse,
            "thread/turns/list": ThreadTurnsListResponse,
            "thread/goal/get": ThreadGoalGetResponse,
        }
        if method not in responses:
            raise ValueError("The monitoring app-server is read-only")
        if method == "thread/read" and (params or {}).get("includeTurns") is not False:
            raise ValueError("Monitoring reads must exclude turn history")
        if method == "thread/turns/list" and (
                (params or {}).get("limit") != 1 or (params or {}).get("itemsView") != "notLoaded"):
            raise ValueError("Monitoring reads require a single turn header without items")
        try:
            result = self.client.request(method, params or {}, response_model=responses[method])
            return result.model_dump(mode="json", by_alias=True, exclude_unset=True)
        except CodexError as error:
            raise BridgeError(f"{method}: {error}") from error

    def read_chat(self, thread_id, desktop, *, include_goal=False):
        """Read only metadata and the latest turn header; never hydrate history.

        An unloaded process can label a running turn interrupted. Only a
        persisted completion timestamp proves it ended. The desktop supplies
        current model settings and retains ownership of every continuation.
        """
        from resume_queue import Chat
        metadata = self.request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
        settings = desktop.settings(thread_id)
        turns = self.request("thread/turns/list", {
            "threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded",
        })["data"]
        goal = self.request("thread/goal/get", {"threadId": thread_id}).get("goal") if include_goal else None
        return Chat.from_headers(metadata, turns, settings, goal)

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

    def settings(self, thread_id, *, before_open=None):
        """Get current model settings, optionally opening a selected dormant chat.

        Opening uses the app's ordinary deep link and can change the visible tab.
        It does not submit a prompt. Callers must exclude archived chats first,
        or supply before_open to validate only when opening is necessary.
        """
        try:
            reply = self.request("thread-follower-read-model-settings", {"conversationId": thread_id})
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
                    reply = self.request("thread-follower-read-model-settings", {"conversationId": thread_id})
                    break
                except BridgeError:
                    if time.monotonic() >= deadline:
                        raise BridgeError("Open the queued chat in Codex; its owner is unavailable") from error
                    time.sleep(0.5)
        settings = reply.get("settings")
        if not isinstance(settings, dict):
            raise BridgeError("Codex desktop model settings are unavailable")
        return settings

    def snapshot(self, thread_id, *, before_open=None):
        """Read legacy anchors only, bounded by the existing 32 MiB circuit breaker."""
        self.snapshots.pop(thread_id, None)
        self.settings(thread_id, before_open=before_open)
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
