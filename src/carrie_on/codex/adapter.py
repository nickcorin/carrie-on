"""Compose Codex reads and desktop operations behind the application protocol."""

from dataclasses import dataclass
from uuid import UUID

from carrie_on.codex.client import CodexConnections
from carrie_on.conversation import Chat, ChatPage, ChatSummary
from carrie_on.ports import CodexUnavailable
from carrie_on.usage import UsageSnapshot


@dataclass(frozen=True)
class CodexAdapter:
    connections: CodexConnections

    def read_usage(self) -> UsageSnapshot:
        with self.connections.app_server() as server:
            return server.usage()

    def list_chats(
        self, search: str = "", *, cursor: str | None = None, limit: int = 25
    ) -> ChatPage:
        with self.connections.app_server() as server:
            return server.list_chats(search, cursor=cursor, limit=limit)

    def list_chat_summaries(self, chat_ids: tuple[UUID, ...]) -> tuple[ChatSummary, ...]:
        summaries = []
        with self.connections.app_server() as server:
            for chat_id in chat_ids:
                try:
                    summaries.append(server.get_chat_summary(str(chat_id)))
                except CodexUnavailable:
                    continue
        return tuple(summaries)

    def get_chat(
        self, chat_id: UUID, *, open_if_needed: bool = False, include_goal: bool = True
    ) -> Chat | None:
        with self.connections.app_server() as server:
            if not any(chat.id == str(chat_id) for chat in server.list_all_chats()):
                return None
            with self.connections.desktop(open_if_needed=open_if_needed) as desktop:
                settings = desktop.read_settings(str(chat_id))
                return server.get_chat(str(chat_id), settings, include_goal=include_goal)

    def has_message(self, chat_id: UUID, message_id: UUID) -> bool:
        with self.connections.app_server() as server:
            return server.has_message(str(chat_id), str(message_id))

    def send(self, chat_id: UUID, instructions: str, message_id: UUID) -> None:
        with self.connections.desktop() as desktop:
            desktop.continue_chat(str(chat_id), instructions, str(message_id))

    def connected(self) -> bool:
        try:
            with self.connections.desktop(timeout=3):
                return True
        except CodexUnavailable:
            return False
