"""Read Codex state through the SDK and send continuations through the desktop."""

import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from openai_codex.client import CodexClient as SDKClient, CodexConfig
from openai_codex.errors import CodexError
from openai_codex.generated.v2_all import (
    CodexErrorInfoValue, GetAccountRateLimitsResponse, ThreadTurnsListResponse,
    ThreadGoalGetResponse, ThreadGoalStatus, ThreadItemsListResponse, TurnStatus,
)

from app_runtime import VERSION


class BridgeError(RuntimeError):
    """A Codex connection failed. A failed send may have been accepted."""


class OwnerUnavailable(BridgeError):
    """No desktop client owns the requested chat."""


@dataclass(frozen=True)
class Chat:
    """The latest accepted Codex turn and its readiness for continuation."""

    thread_id: str
    title: str
    turn_id: str
    model: str
    status: str
    busy: bool
    blocked: bool
    quota_failed: bool


def _chat_summary(thread, fallback_title=None):
    title = thread.name or (thread.preview if fallback_title is None else fallback_title)
    return {"id": thread.id, "title": title,
            "cwd": thread.cwd.root, "last_activity": thread.updated_at}


class CodexClient:
    """Read Codex chats and quota through an SDK session opened by connect().

    SDK request failures raise JsonRpcError for callers to handle per operation.
    """

    def __init__(self, client):
        self._client = client

    @classmethod
    @contextmanager
    def connect(cls, executable="codex"):
        """Open an SDK session and close it on exit, including initialization failure.

        Uncaught SDK errors become BridgeError after cleanup.
        """
        config = CodexConfig(
            codex_bin=shutil.which(executable) or executable,
            client_name="carrie_on", client_version=VERSION,
            # Read persisted goals even when the interactive configuration disables them.
            config_overrides=("features.goals=true",),
        )
        try:
            with SDKClient(config) as client:
                client.initialize()
                yield cls(client)
        except CodexError as error:
            raise BridgeError(str(error)) from error

    def read_chat_summary(self, thread_id, *, fallback_title=None):
        """Return a chat summary without turn contents.

        The dictionary contains id, title, cwd, and last_activity. cwd is the
        working directory. last_activity is the update time in Unix seconds.
        An unnamed chat uses fallback_title when supplied, otherwise its preview.
        """
        thread = self._client.thread_read(thread_id, include_turns=False).thread
        return _chat_summary(thread, fallback_title)

    def read_chat(self, thread_id, desktop, *, include_goal=False, open_if_needed=False):
        """Read the latest turn header and desktop settings without turn contents.

        Missing completion evidence counts as busy. include_goal adds goal blockers.
        open_if_needed permits opening an unarchived chat without a desktop owner.
        """
        thread = self._client.thread_read(thread_id, include_turns=False).thread
        title = thread.name or thread.preview or thread.id
        try:
            settings = desktop.read_settings(thread_id)
        except OwnerUnavailable:
            if not open_if_needed:
                raise
            if not self.contains_chat(thread_id, title):
                raise ValueError("Chat is archived or absent from the local catalog; open it and retry")
            settings = desktop.open_chat(thread_id)

        turns = self._client.request("thread/turns/list", {
            "threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded",
        }, response_model=ThreadTurnsListResponse).data
        model = settings.get("model")
        if len(turns) != 1 or not turns[0].id or not thread.id or not isinstance(model, str) or not model:
            raise ValueError("Chat has no identifiable latest turn or model; open it in Codex first")
        turn = turns[0]

        goal = None
        if include_goal:
            goal = self._client.request("thread/goal/get", {"threadId": thread_id},
                                        response_model=ThreadGoalGetResponse).goal

        busy = turn.status == TurnStatus.in_progress or not turn.completed_at or turn.completed_at <= 0
        blocked = settings.get("resumeState") != "resumed" or (goal is not None and goal.status in {
            ThreadGoalStatus.paused, ThreadGoalStatus.budget_limited, ThreadGoalStatus.blocked})
        quota_failed = (turn.error is not None and turn.error.codex_error_info is not None
                        and turn.error.codex_error_info.root == CodexErrorInfoValue.usage_limit_exceeded)
        return Chat(
            thread_id=thread.id, title=title, turn_id=turn.id, model=model, status=turn.status.value,
            busy=busy, blocked=blocked, quota_failed=quota_failed,
        )

    def read_usage(self):
        """Return current account and quota data as a dictionary."""
        result = self._client.request("account/rateLimits/read", {}, response_model=GetAccountRateLimitsResponse)
        return result.model_dump(mode="json", by_alias=True, exclude_unset=True)

    def find_legacy_turn(self, thread_id, message_id, *, latest_only=True):
        """Return the accepted turn ID for this client-message ID, or None.

        latest_only restricts matching to the latest turn. False searches older
        turns to confirm delivery. Reads include opening prompts, not full turns.
        """
        cursor = None
        seen = set()
        while True:
            params = {"threadId": thread_id, "limit": 1 if latest_only else 100,
                      "sortDirection": "desc", "itemsView": "notLoaded"}
            if cursor is not None:
                params["cursor"] = cursor
            page = self._client.request("thread/turns/list", params, response_model=ThreadTurnsListResponse)
            for turn in page.data:
                if not turn.id:
                    raise ValueError("Codex returned a turn without an ID")
                if self._opening_message_id(thread_id, turn.id) == message_id:
                    return turn.id
            if latest_only or page.next_cursor is None:
                return None
            if page.next_cursor in seen:
                raise ValueError("Codex returned a repeated turn cursor")
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    def _opening_message_id(self, thread_id, turn_id):
        """Read the opening client ID, skipping preceding compaction markers."""
        cursor = None
        seen = set()
        while True:
            params = {"threadId": thread_id, "turnId": turn_id, "limit": 1, "sortDirection": "asc"}
            if cursor is not None:
                params["cursor"] = cursor
            page = self._client.request("thread/items/list", params, response_model=ThreadItemsListResponse)
            for entry in page.data:
                if entry.turn_id != turn_id:
                    raise ValueError("Codex returned an item from a different turn")
                item = entry.item.root
                if item.type == "contextCompaction":
                    continue
                return item.client_id if item.type == "userMessage" else None
            if page.next_cursor is None:
                return None
            if page.next_cursor in seen:
                raise ValueError("Codex returned a repeated item cursor")
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    def contains_chat(self, thread_id, title):
        """Check all unarchived catalog pages matching the saved title and ID."""
        cursor = None
        while True:
            page = self.list_chats(title if title != thread_id else None, cursor=cursor, limit=100)
            if any(thread["id"] == thread_id for thread in page["chats"]):
                return True
            cursor = page["next_cursor"]
            if cursor is None:
                return False

    def list_chats(self, search=None, *, cursor=None, limit=25):
        """Return an unarchived chat page, newest activity first.

        The dictionary contains chats, a list of read_chat_summary() dictionaries,
        and next_cursor, a string or None. None means there are no more pages.
        Callers pass next_cursor as cursor and retain search for subsequent pages.
        Concurrent activity can move chats between pages.
        """
        page = self._client.thread_list({
            "limit": limit, "archived": False, "sourceKinds": [],
            "sortKey": "updated_at", "sortDirection": "desc", "searchTerm": search, "cursor": cursor,
        })
        return {"chats": [_chat_summary(thread) for thread in page.data], "next_cursor": page.next_cursor}


class DesktopClient:
    """Read settings and send continuations through the local desktop socket.

    The context manager opens and closes the connection. Requests time out after
    timeout seconds. A timeout after a send does not establish non-delivery.
    """

    def __init__(self, path=None, timeout=15):
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.path = Path(path) if path else home / "ipc" / "ipc.sock"
        self.timeout = timeout
        self.client_id = "carrie_on"
        self.socket = None

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
            reply = self._request("initialize", {"clientType": "carrie_on"}, version=0)
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
        return message

    def _request(self, method, params, version=2):
        request_id = str(uuid.uuid4())
        message = {"type": "request", "requestId": request_id,
                   "sourceClientId": self.client_id, "version": version,
                   "method": method, "params": params}
        if method != "initialize":
            message["hostId"] = "local"
        self._send(message)
        deadline = time.monotonic() + self.timeout
        while True:
            reply = self._receive(deadline)
            if reply.get("type") != "response" or reply.get("requestId") != request_id:
                continue
            if reply.get("resultType") != "success":
                error = reply.get("error", "request failed")
                if isinstance(error, str) and error.split(":", 1)[0] == "no-client-found":
                    raise OwnerUnavailable(f"{method}: {error}")
                raise BridgeError(f"{method}: {error}")
            if reply.get("method") != method:
                raise BridgeError("Codex desktop response method mismatch")
            return reply["result"]

    def read_settings(self, thread_id):
        """Return desktop settings, or raise OwnerUnavailable if the chat has no owner."""
        reply = self._request("thread-follower-read-model-settings", {"conversationId": thread_id})
        settings = reply.get("settings")
        if not isinstance(settings, dict):
            raise BridgeError("Codex desktop model settings are unavailable")
        return settings

    def open_chat(self, thread_id):
        """Open a chat by UUID and return its settings when a desktop owner appears.

        The caller must first exclude archived chats. Opening may change the
        visible tab. Retries apply only to OwnerUnavailable.
        """
        uuid.UUID(thread_id)
        subprocess.run(["/usr/bin/open", "-g", f"codex://threads/{thread_id}"],
                       check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while True:
            try:
                return self.read_settings(thread_id)
            except OwnerUnavailable as error:
                if time.monotonic() >= deadline:
                    raise OwnerUnavailable("Open the queued chat in Codex; its owner is unavailable") from error
                time.sleep(0.5)

    def continue_chat(self, thread_id, prompt, message_id):
        """Send a continuation with the chat's settings and return its accepted turn ID.

        The caller must save message_id before sending. A failed request may have
        been accepted, so failure alone must not trigger a resend.
        """
        receipt = self._request("thread-follower-start-turn", {
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
        return turn_id
