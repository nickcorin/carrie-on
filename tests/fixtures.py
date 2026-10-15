"""Provide synthetic Codex responses without contacting user chats."""

import time
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


def snapshot(status="failed", message_id="original", runtime="idle", error="usageLimitExceeded"):
    return {
        "id": CHAT_ID,
        "title": "Finish feature",
        "latestTurnStartMessageId": message_id,
        "latestModel": "model-a",
        "resumeState": "resumed",
        "requests": [],
        "threadRuntimeStatus": {"type": runtime},
        "turnHistory": {
            "kind": "canonical",
            "history": {
                "entitiesByKey": {
                    "latest": {
                        "turnId": "turn-a",
                        "status": status,
                        "error": {"codexErrorInfo": error},
                        "params": {"clientUserMessageId": message_id},
                    },
                }
            },
        },
    }


class CodexFixture:
    def __init__(self):
        self.limits = usage(0, 200)
        self.chat = snapshot()
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

    def snapshot(self, _thread, _desktop=None, **_options):
        return Chat.parse(self.chat)

    def has_message(self, _thread, message_id):
        return message_id in Chat.parse(self.chat).seen_message_ids

    def threads(self, _search):
        return (ChatSummary(id=CHAT_ID, title="Finish feature", cwd=None, last_activity=None),)

    def continue_chat(self, thread_id, prompt, message_id):
        self.delivered.append({"thread_id": thread_id, "prompt": prompt, "message_id": message_id})
        if self.lose_reply:
            raise CodexUnavailable("Reply lost")
        self.chat = snapshot("inProgress", message_id, "active")
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

    def threads(self, search=None):
        title = 'A chat with <script> & "quotes"'
        if search and search not in title:
            return []
        return (
            ChatSummary(id=CHAT_ID, title=title, cwd="/projects/example", last_activity=1791900000),
        )

    def thread_page(self, search=None, cursor=None, limit=25):
        return ChatPage(tuple(self.threads(search)), None)

    def read_chat(self, chat_id):
        if chat_id != CHAT_ID:
            raise CodexUnavailable("Unknown fixture chat")
        return self.threads()[0]

    def snapshot(self, thread_id, _desktop=None, **_options):
        result = snapshot("completed")
        result["id"] = thread_id
        result["title"] = self.threads()[0].title
        return Chat.parse(result)


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
