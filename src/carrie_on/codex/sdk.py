"""Read Codex data through SDK calls with timeouts and page limits.

The client does not subscribe to desktop history.
"""

import shutil
import threading
import time
from collections.abc import Callable
from importlib.metadata import version
from typing import TYPE_CHECKING, TypeVar

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.errors import CodexError
from openai_codex.generated.v2_all import (
    GetAccountRateLimitsResponse,
    ThreadGoalGetResponse,
    ThreadGoalStatus,
    ThreadItemsListResponse,
    ThreadListResponse,
    ThreadReadResponse,
    ThreadTurnsListResponse,
    TurnStatus,
)
from pydantic import ValidationError

from carrie_on.codex.models import (
    Chat,
    ChatPage,
    ChatSummary,
    CodexProtocolError,
    CodexUnavailable,
    UsageSnapshot,
)

if TYPE_CHECKING:
    from carrie_on.codex.client import Desktop

T = TypeVar("T")


class AppServer:
    """Close the SDK session when a request exceeds its timeout or the supplied deadline."""

    def __init__(self, executable="codex", timeout=30, deadline=None, codex_home=None):
        self.timeout, self.deadline = timeout, deadline
        self._client = CodexClient(
            CodexConfig(
                codex_bin=shutil.which(executable) or executable,
                client_name="carrie_on",
                client_version=version("carrie-on"),
                config_overrides=("features.goals=true",),
                env={"CODEX_HOME": codex_home} if codex_home else None,
            )
        )

    def __enter__(self):
        try:
            self._client.start()
            self._call(self._client.initialize)
        except BaseException:
            self._client.close()
            raise
        return self

    def __exit__(self, *_):
        self._client.close()

    def _call(self, operation: Callable[[], T]) -> T:
        timeout = self.timeout
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        if timeout <= 0:
            raise CodexUnavailable("Codex operation deadline expired")
        timer = threading.Timer(timeout, self._client.close)
        timer.daemon = True
        timer.start()
        try:
            return operation()
        except ValidationError as error:
            raise CodexProtocolError("Codex returned an unsupported response") from error
        except (CodexError, OSError) as error:
            raise CodexUnavailable(str(error) or type(error).__name__) from error
        finally:
            timer.cancel()

    def usage(self) -> UsageSnapshot:
        response = self._call(
            lambda: self._client.request(
                "account/rateLimits/read", {}, response_model=GetAccountRateLimitsResponse
            )
        )
        return UsageSnapshot.parse(
            response.model_dump(mode="json", by_alias=True, exclude_unset=True)
        )

    def thread_page(self, search=None, *, cursor=None, limit=25) -> ChatPage:
        response = self._call(
            lambda: self._client.request(
                "thread/list",
                {
                    "limit": limit,
                    "archived": False,
                    "sourceKinds": [],
                    "sortKey": "updated_at",
                    "sortDirection": "desc",
                    "searchTerm": search,
                    "cursor": cursor,
                },
                response_model=ThreadListResponse,
            )
        )
        return ChatPage(
            tuple(self._summary(thread) for thread in response.data), response.next_cursor
        )

    @staticmethod
    def _summary(thread) -> ChatSummary:
        return ChatSummary(
            id=thread.id,
            title=thread.name or thread.preview,
            cwd=thread.cwd.root,
            last_activity=thread.updated_at,
        )

    def threads(self, search=None) -> tuple[ChatSummary, ...]:
        """Read every matching catalog page and reject repeated cursors."""
        entries: list[ChatSummary] = []
        seen: set[str] = set()
        cursor = None
        while True:
            page = self.thread_page(search, cursor=cursor, limit=100)
            entries.extend(page.items)
            if page.next_cursor is None:
                return tuple(entries)
            if page.next_cursor in seen:
                raise CodexProtocolError("Codex returned a repeated catalog cursor")
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    def read_chat(self, chat_id: str) -> ChatSummary:
        response = self._call(
            lambda: self._client.request(
                "thread/read",
                {
                    "threadId": chat_id,
                    "includeTurns": False,
                },
                response_model=ThreadReadResponse,
            )
        )
        return self._summary(response.thread)

    def snapshot(
        self,
        chat_id: str,
        desktop: "Desktop",
        *,
        receipt_id: str | None = None,
        include_goal: bool = True,
        before_open: Callable[[], None] | None = None,
    ) -> Chat:
        """Read the latest turn's metadata, opening message ID, and desktop settings.

        Treat the chat as busy while the turn is in progress or has no positive
        completion timestamp. Turn pages omit items. Read item pages until the
        first item other than context compaction, and use its client ID if it is
        a user message. Scan older turns only when receipt_id was supplied and
        is absent from the latest turn.
        """
        summary = self.read_chat(chat_id)
        settings = desktop.read_settings(chat_id, before_open=before_open)
        page = self._turns(chat_id, limit=1)
        if len(page.data) != 1 or not page.data[0].id:
            raise CodexProtocolError("Chat has no identifiable latest turn")
        turn = page.data[0]
        message_id = self._opening_message_id(chat_id, turn.id)
        model = settings.get("model")
        if not isinstance(model, str) or not model:
            raise CodexProtocolError("Chat has no identifiable model")
        goal = (
            self._call(
                lambda: self._client.request(
                    "thread/goal/get",
                    {
                        "threadId": chat_id,
                    },
                    response_model=ThreadGoalGetResponse,
                )
            ).goal
            if include_goal
            else None
        )
        seen = {message_id} if message_id else set()
        if receipt_id and receipt_id not in seen and self.has_message(chat_id, receipt_id):
            seen.add(receipt_id)
        return Chat(
            thread_id=summary.id,
            title=summary.title or summary.id,
            message_id=message_id,
            turn_id=turn.id,
            model=model,
            status=turn.status.value,
            busy=turn.status == TurnStatus.in_progress
            or not turn.completed_at
            or turn.completed_at <= 0,
            blocked=settings.get("resumeState") != "resumed"
            or bool(
                goal
                and goal.status
                in {
                    ThreadGoalStatus.paused,
                    ThreadGoalStatus.budget_limited,
                    ThreadGoalStatus.blocked,
                }
            ),
            seen_message_ids=frozenset(seen),
        )

    def _turns(self, chat_id, *, limit=100, cursor=None):
        return self._call(
            lambda: self._client.request(
                "thread/turns/list",
                {
                    "threadId": chat_id,
                    "limit": limit,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                    "cursor": cursor,
                },
                response_model=ThreadTurnsListResponse,
            )
        )

    def has_message(self, chat_id: str, message_id: str) -> bool:
        """Look for an accepted message in pages of turn metadata and opening messages."""
        cursor, seen = None, set()
        while True:
            page = self._turns(chat_id, cursor=cursor)
            for turn in page.data:
                if not turn.id:
                    raise CodexProtocolError("Codex returned a turn without an ID")
                if self._opening_message_id(chat_id, turn.id) == message_id:
                    return True
            if page.next_cursor is None:
                return False
            if page.next_cursor in seen:
                raise CodexProtocolError("Codex returned a repeated turn cursor")
            seen.add(page.next_cursor)
            cursor = page.next_cursor

    def _opening_message_id(self, chat_id: str, turn_id: str) -> str | None:
        cursor, seen = None, set()
        while True:
            page = self._call(
                lambda: self._client.request(
                    "thread/items/list",
                    {
                        "threadId": chat_id,
                        "turnId": turn_id,
                        "limit": 1,
                        "sortDirection": "asc",
                        "cursor": cursor,
                    },
                    response_model=ThreadItemsListResponse,
                )
            )
            for entry in page.data:
                if entry.turn_id != turn_id:
                    raise CodexProtocolError("Codex returned an item from a different turn")
                item = entry.item.root
                if item.type == "contextCompaction":
                    continue
                return item.client_id if item.type == "userMessage" else None
            if page.next_cursor is None:
                return None
            if page.next_cursor in seen:
                raise CodexProtocolError("Codex returned a repeated item cursor")
            seen.add(page.next_cursor)
            cursor = page.next_cursor
