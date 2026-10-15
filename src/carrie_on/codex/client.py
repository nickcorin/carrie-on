"""Model-free Codex reads and desktop-owned continuation.

The app-server subprocess is restricted to read methods. Desktop dispatch uses
the versioned, same-user IPC interface; no authentication files are inspected.
Both clients own their connection and must be used as context managers.
"""

import json
import os
import socket
import stat
import struct
import subprocess
import time
import uuid
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Callable, ParamSpec, TypeVar

from carrie_on.codex.models import (
    CodexProtocolError,
    CodexUnavailable,
    object_value,
    text_value,
)
from carrie_on.codex.sdk import AppServer

P = ParamSpec("P")
T = TypeVar("T")


def transport_errors(operation: Callable[P, T]) -> Callable[P, T]:
    """Translate only transport failures; programming exceptions retain their traceback."""

    @wraps(operation)
    def perform(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return operation(*args, **kwargs)
        except json.JSONDecodeError as error:
            raise CodexProtocolError("Codex returned invalid JSON") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise CodexUnavailable(str(error) or type(error).__name__) from error

    return perform


@dataclass(frozen=True)
class CodexConnections:
    """Configured connection creation; callers own each context-managed connection."""

    executable: str = "codex"
    socket_path: Path | None = None
    codex_home: str | None = None

    def app_server(self, *, deadline: float | None = None) -> "AppServer":
        return AppServer(self.executable, deadline=deadline, codex_home=self.codex_home)

    def desktop(
        self, *, open_if_needed: bool = False, deadline: float | None = None, timeout: float = 15
    ) -> "Desktop":
        path = self.socket_path
        if path is None and self.codex_home:
            path = Path(self.codex_home) / "ipc" / "ipc.sock"
        return Desktop(path, open_if_needed=open_if_needed, deadline=deadline, timeout=timeout)


def remaining_time(timeout, deadline):
    remaining = min(timeout, deadline - time.monotonic()) if deadline is not None else timeout
    if remaining <= 0:
        raise CodexUnavailable("Codex operation deadline expired")
    return remaining


class Desktop:
    """Coordinate with the running desktop app over its private IPC socket.

    Read only compact settings and ask the owning app to start a turn.
    Never subscribe to conversation history or claim ownership of another chat.
    """

    def __init__(self, path=None, timeout=15, open_if_needed=False, deadline=None):
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.path = Path(path) if path else home / "ipc" / "ipc.sock"
        self.timeout = timeout
        self.deadline = deadline
        self.open_if_needed = open_if_needed
        self.client_id = "carrie_on"
        self.socket: socket.socket | None = None

    @transport_errors
    def __enter__(self):
        info = self.path.lstat()
        parent = self.path.parent.stat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o022
        ):
            raise CodexUnavailable("Codex IPC socket must be in a private, user-owned directory")
        self.socket = socket.socket(socket.AF_UNIX)
        try:
            self.socket.settimeout(remaining_time(self.timeout, self.deadline))
            self.socket.connect(str(self.path))
            reply = self.request("initialize", {"clientType": "carrie_on"}, version=0)
            self.client_id = text_value(reply.get("clientId"), "desktop client ID")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        if self.socket is not None:
            self.socket.close()

    def _send(self, message):
        assert self.socket is not None
        payload = json.dumps(message).encode()
        self.socket.settimeout(remaining_time(self.timeout, self.deadline))
        self.socket.sendall(struct.pack("<I", len(payload)) + payload)

    def _read_bytes(self, count, deadline):
        assert self.socket is not None
        chunks = bytearray()
        while len(chunks) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexUnavailable("Codex desktop response timed out")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv(count - len(chunks))
            if not chunk:
                raise CodexUnavailable("Codex desktop disconnected")
            chunks.extend(chunk)
        return chunks

    def _receive(self, deadline):
        length = struct.unpack("<I", self._read_bytes(4, deadline))[0]
        if not 0 < length <= 32 * 1024 * 1024:
            raise CodexUnavailable("Invalid or oversized Codex desktop frame")
        message = object_value(json.loads(self._read_bytes(length, deadline)), "desktop message")
        if message.get("type") == "client-discovery-request":
            self._send(
                {
                    "type": "client-discovery-response",
                    "requestId": message["requestId"],
                    "response": {"canHandle": False},
                }
            )
        return message

    @transport_errors
    def request(self, method, params, version=2, host="local"):
        """Send a versioned IPC request. Timeouts do not establish non-delivery."""
        request_id = str(uuid.uuid4())
        message = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id,
            "version": version,
            "method": method,
            "params": params,
        }
        if method != "initialize":
            message["hostId"] = host
        self._send(message)
        deadline = min(time.monotonic() + self.timeout, self.deadline or float("inf"))
        while True:
            reply = self._receive(deadline)
            if reply.get("type") != "response" or reply.get("requestId") != request_id:
                continue
            if reply.get("resultType") != "success":
                raise CodexUnavailable(f"{method}: {reply.get('error', 'request failed')}")
            if reply.get("method") != method:
                raise CodexProtocolError("Codex desktop response method mismatch")
            return object_value(reply.get("result"), "desktop result")

    @transport_errors
    def read_settings(
        self, thread_id: str, *, before_open: Callable[[], None] | None = None
    ) -> dict:
        """Read compact settings; open a dormant chat only after catalog validation."""
        try:
            result = self.request(
                "thread-follower-read-model-settings", {"conversationId": thread_id}
            )
        except CodexUnavailable as error:
            if not self.open_if_needed or "no-client-found" not in str(error):
                raise
            uuid.UUID(thread_id)
            if before_open is not None:
                before_open()
            subprocess.run(
                ["/usr/bin/open", "-g", f"codex://threads/{thread_id}"],
                check=True,
                timeout=remaining_time(10, self.deadline),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = min(time.monotonic() + 20, self.deadline or float("inf"))
            while True:
                try:
                    result = self.request(
                        "thread-follower-read-model-settings", {"conversationId": thread_id}
                    )
                    break
                except CodexUnavailable as retry:
                    if "no-client-found" not in str(retry):
                        raise
                    if time.monotonic() >= deadline:
                        raise CodexUnavailable(
                            "Open the queued chat in Codex; its owner is unavailable"
                        ) from error
                    time.sleep(0.5)
        return object_value(result.get("settings"), "desktop model settings")

    def continue_chat(self, thread_id: str, prompt: str, message_id: str) -> str:
        """Ask the owning app to start a turn with its existing chat settings.

        The caller must durably record message_id before calling. Errors may
        occur after delivery; reconcile live state before any further attempt.
        """
        receipt = self.request(
            "thread-follower-start-turn",
            {
                "conversationId": thread_id,
                "turnStart": {
                    "request": {
                        "threadId": thread_id,
                        "clientUserMessageId": message_id,
                        "input": [{"type": "text", "text": prompt, "text_elements": []}],
                    },
                    "context": {"inheritThreadSettings": True},
                },
            },
            version=3,
        )
        result = receipt.get("result")
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise CodexUnavailable("Desktop returned no accepted turn ID; delivery is uncertain")
        return turn_id
