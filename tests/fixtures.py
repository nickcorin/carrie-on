"""Provide synthetic Codex responses without contacting user chats."""

import time
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

from carrie_on.codex.models import Chat, ChatPage, ChatSummary, CodexUnavailable, UsageSnapshot
from carrie_on.queue import QueueEntry


def usage(used=100, reset=100, secondary=None, account="account-a"):
    return {
        "accountId": account,
        "ordinaryUsageAllowed": used < 100,
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"usedPercent": used, "resetsAt": reset},
                "secondary": secondary,
            }
        },
    }


def chat(**changes):
    return replace(
        Chat(
            thread_id=CHAT_ID,
            title="Finish feature",
            message_id="original",
            turn_id="turn-a",
            model="model-a",
            status="failed",
            busy=False,
            blocked=False,
        ),
        **changes,
    )


class CodexFixture:
    def __init__(self):
        self.limits = usage(0, 200)
        self.chat = chat()
        self.delivered = []
        self.lose_reply = False
        self.usage_error = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def usage(self):
        if self.usage_error:
            raise CodexUnavailable("Usage unavailable")
        return UsageSnapshot.parse(self.limits)

    def get_chat(self, _thread, _desktop=None, **_options):
        return self.chat

    def has_message(self, _thread, message_id):
        return message_id == self.chat.message_id

    def list_all_chats(self, _search):
        return (ChatSummary(id=CHAT_ID, title="Finish feature", cwd=None, last_activity=None),)

    def continue_chat(self, thread_id, prompt, message_id):
        self.delivered.append({"thread_id": thread_id, "prompt": prompt, "message_id": message_id})
        if self.lose_reply:
            raise CodexUnavailable("Reply lost")
        self.chat = chat(status="inProgress", message_id=message_id, busy=True)
        return "continued"


CHAT_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


class LocalCodex:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def usage(self):
        return UsageSnapshot.parse(usage(25, int(time.time()) + 3600))

    def list_all_chats(self, search=None):
        title = 'A chat with <script> & "quotes"'
        if search and search not in title:
            return []
        return (
            ChatSummary(id=CHAT_ID, title=title, cwd="/projects/example", last_activity=1791900000),
        )

    def list_chats(self, search=None, cursor=None, limit=25):
        return ChatPage(tuple(self.list_all_chats(search)), None)

    def get_chat_summary(self, chat_id):
        if chat_id != CHAT_ID:
            raise CodexUnavailable("Unknown fixture chat")
        return self.list_all_chats()[0]

    def get_chat(self, thread_id, _desktop=None, **_options):
        return chat(
            status="completed",
            thread_id=thread_id,
            title=self.list_all_chats()[0].title,
        )


def instant(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


def queue_entry(**changes):
    fields = dict(
        id=uuid4(),
        chat_id=UUID(CHAT_ID),
        title="Task A",
        account_id="account-a",
        limit_id="codex",
        anchor_message_id="original",
        model="model-a",
        instructions="Continue",
        reset_windows={"primary": 100},
        enrolled_while_busy=False,
        queued_at=instant(50),
        updated_at=instant(50),
    )
    fields.update(changes)
    return QueueEntry(**fields)
