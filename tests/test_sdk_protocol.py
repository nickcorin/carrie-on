"""Test SDK reads through a local process that simulates the Codex app-server."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from carrie_on.codex.models import CodexProtocolError, CodexUnavailable
from carrie_on.codex.sdk import AppServer


class Settings:
    def read_settings(self, thread_id, **kwargs):
        return {"model": "model-a", "resumeState": "resumed"}


class SDKProtocol(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.executable = Path(directory.name) / "codex"
        self.state = Path(directory.name) / "queue.json"
        self.request_log = Path(directory.name) / "requests.jsonl"

    def fixture(self, mode="normal", *, turn=None):
        self.executable.write_text(
            f"#!{sys.executable}\n"
            + """
import json, sys
mode = """
            + repr(mode)
            + """
turn_override = """
            + repr(turn)
            + """
request_log = """
            + repr(str(self.request_log))
            + """
turn_reads = 0
thread = {"id":"thread-a", "name":"Finish feature", "preview":"", "modelProvider":"openai",
          "createdAt":1,"updatedAt":2,"cwd":"/tmp","cliVersion":"0.162.1","ephemeral":False,
          "sessionId":"session-a","source":"appServer","status":{"type":"notLoaded"},"turns":[]}
if mode in {"unnamed", "untitled"}:
    thread.update(name=None, preview="Finish feature" if mode == "unnamed" else "")
for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    if method == "initialized": continue
    if method == "initialize":
        if mode == "initialize_error":
            import os
            with open(request_log, "a") as log:
                log.write(json.dumps({"pid":os.getpid()}) + "\\n")
            print(json.dumps({"id":message["id"],"error":{"code":-32600,"message":"Initialization failed"}}),flush=True)
            continue
        result = {"userAgent":"codex/0.162.1"}
    elif mode == "error":
        print(json.dumps({"id":message["id"],"error":{"code":-32602,"message":"Missing thread"}}),flush=True)
        continue
    elif method == "thread/read":
        assert message["params"]["includeTurns"] is False
        thread_id = message["params"]["threadId"]
        if thread_id == "thread-a" and mode in {"request_error", "unknown_request_error", "disconnect"}:
            with open(request_log, "a") as log:
                log.write(json.dumps({"method":method,"params":message["params"],"failure":mode}) + "\\n")
            if mode == "disconnect": sys.exit(0)
            code = -32602 if mode == "request_error" else 12345
            print(json.dumps({"id":message["id"],"error":{"code":code,"message":"Missing thread-a"}}),flush=True)
            continue
        result = {"thread":dict(thread, id=thread_id)}
    elif method == "thread/turns/list":
        turn_reads += 1
        assert message["params"]["threadId"] in {"thread-a", "thread-b"}
        assert message["params"]["itemsView"] == "notLoaded"
        assert message["params"]["sortDirection"] == "desc"
        turn = {"id":"turn-a","status":"interrupted","completedAt":None,"items":[],"itemsView":"notLoaded"}
        if mode.startswith("legacy") or mode in {"quota", "unnamed", "untitled", "paused", "request_error", "unknown_request_error", "disconnect"}: turn.update(status="failed",completedAt=99,error={"message":"Quota exhausted","codexErrorInfo":"usageLimitExceeded"})
        if mode == "completed": turn.update(status="completed",completedAt=99)
        if mode == "stopped": turn.update(completedAt=99)
        if mode == "changed": turn.update(id="turn-b",status="completed",completedAt=99)
        if mode == "legacy_race" and turn_reads > 2: turn.update(id="turn-b")
        if turn_override is not None: turn.update(turn_override)
        result = {"data":[turn]}
        if mode == "legacy_recovery":
            if message["params"].get("cursor"):
                result = {"data":[dict(turn, id="earlier-turn")]}
            else:
                result["nextCursor"] = "older-turns"
    elif method == "thread/items/list":
        params = message["params"]
        assert params["limit"] == 1 and params["sortDirection"] == "asc"
        turn_id = params["turnId"]
        item = {"type":"userMessage","id":"item-a","clientId":"original",
                "content":[{"type":"text","text":"Finish feature","text_elements":[]}]}
        cursor = None
        if mode in {"legacy_compaction", "legacy_cursor"} and (not params.get("cursor") or mode == "legacy_cursor"):
            item = {"type":"contextCompaction","id":"compaction"}
            cursor = "opening-message"
        if mode == "legacy_missing": item["clientId"] = None
        if mode == "legacy_changed": item["clientId"] = "new-message"
        if mode == "legacy_recovery" and turn_id != "earlier-turn": item["clientId"] = "later-message"
        if mode == "legacy_wrong_turn": turn_id = "different-turn"
        if mode == "legacy_non_user": item = {"type":"agentMessage","id":"agent-a","text":"Done"}
        result = {"data":[{"turnId":turn_id,"item":item}],"nextCursor":cursor}
        if mode == "legacy_empty": result = {"data":[],"nextCursor":None}
    elif method == "thread/goal/get":
        if "features.goals=true" not in sys.argv:
            print(json.dumps({"id":message["id"],"error":{"code":-32600,"message":"goals feature is disabled"}}),flush=True)
            continue
        goal = None if mode != "paused" else {"threadId":"thread-a","objective":"Finish feature", "status":"paused",
            "createdAt":1,"updatedAt":2,"timeUsedSeconds":0,"tokensUsed":0}
        result = {"goal":goal}
    elif method == "thread/list":
        search = message["params"].get("searchTerm") or ""
        data = [thread] if search in (thread.get("name") or thread["preview"]) else []
        if data and mode in {"request_error", "unknown_request_error", "disconnect"}:
            data.append(dict(thread, id="thread-b"))
        result = {"data":data,"nextCursor":None}
        if mode == "pages" and message["params"].get("cursor") is None:
            result = {"data":[dict(thread, id="another-thread")],"nextCursor":"page2"}
    elif method == "account/rateLimits/read":
        bucket = {"limitId":"codex","primary":{"usedPercent":0,"windowDurationMins":300,"resetsAt":200}}
        result = {"accountId":"account-a","ordinaryUsageAllowed":True,"rateLimits":bucket,"rateLimitsByLimitId":{"codex":bucket}}
    else: raise RuntimeError("Unexpected request: " + method)
    reply = json.dumps({"id":message["id"],"result":result})
    with open(request_log, "a") as log:
        log.write(json.dumps({"method":method,"params":message.get("params"),"reply_bytes":len(reply.encode())}) + "\\n")
    print(reply,flush=True)
"""
        )
        self.executable.chmod(0o700)

    def test_completion_evidence_distinguishes_running_and_ended_work(self):
        for status in ("failed", "completed", "interrupted", "inProgress"):
            for completed_at in (None, 0, 99):
                with self.subTest(status=status, completed_at=completed_at):
                    self.fixture(turn={"status": status, "completedAt": completed_at})
                    with AppServer(str(self.executable)) as server:
                        chat = server.snapshot("thread-a", Settings())
                    self.assertEqual(chat.busy, status == "inProgress" or not completed_at)
                    self.assertEqual((chat.message_id, chat.model), ("original", "model-a"))

    def test_compact_reads_capture_ended_work_without_loading_history(self):
        self.fixture("quota")
        with AppServer(str(self.executable)) as server:
            chat = server.snapshot("thread-a", Settings())
        self.assertEqual(
            (chat.title, chat.busy, chat.blocked, chat.status),
            ("Finish feature", False, False, "failed"),
        )
        reads = [json.loads(line) for line in self.request_log.read_text().splitlines()]
        self.assertEqual(
            [r["method"] for r in reads],
            [
                "initialize",
                "thread/read",
                "thread/turns/list",
                "thread/items/list",
                "thread/goal/get",
            ],
        )
        self.assertEqual(
            next(r for r in reads if r["method"] == "thread/read")["params"]["includeTurns"], False
        )
        self.assertEqual(
            next(r for r in reads if r["method"] == "thread/turns/list")["params"]["itemsView"],
            "notLoaded",
        )

    def test_goal_blocks_dispatch_without_changing_desktop_state(self):
        self.fixture("paused")
        with AppServer(str(self.executable)) as server:
            self.assertTrue(server.snapshot("thread-a", Settings()).blocked)
            self.assertFalse(server.snapshot("thread-a", Settings(), include_goal=False).blocked)

    def test_catalog_membership_reaches_later_pages(self):
        self.fixture("pages")
        with AppServer(str(self.executable)) as server:
            self.assertEqual([chat.id for chat in server.threads()], ["another-thread", "thread-a"])

    def test_unnamed_chat_uses_preview(self):
        self.fixture("unnamed")
        with AppServer(str(self.executable)) as server:
            self.assertEqual(server.read_chat("thread-a").title, "Finish feature")
            self.assertEqual(server.thread_page().items[0].cwd, "/tmp")

    def test_receipt_can_be_found_on_an_older_turn(self):
        self.fixture("legacy_recovery")
        with AppServer(str(self.executable)) as server:
            chat = server.snapshot("thread-a", Settings(), receipt_id="original")
        self.assertEqual(chat.message_id, "later-message")
        self.assertEqual(chat.seen_message_ids, frozenset({"later-message", "original"}))

    def test_compaction_marker_does_not_hide_opening_message(self):
        self.fixture("legacy_compaction")
        with AppServer(str(self.executable)) as server:
            self.assertEqual(server.snapshot("thread-a", Settings()).message_id, "original")

    def test_unproven_opening_identity_fails_closed(self):
        for mode in ("legacy_wrong_turn", "legacy_cursor"):
            with self.subTest(mode=mode):
                self.fixture(mode)
                with AppServer(str(self.executable)) as server:
                    with self.assertRaises(CodexProtocolError):
                        server.snapshot("thread-a", Settings())
        self.fixture("quota")
        with AppServer(str(self.executable)) as server:
            self.assertEqual(server.snapshot("thread-a", Settings()).message_id, "original")

    def test_turn_identity_is_available_when_opening_message_has_no_client_id(self):
        for mode in ("legacy_missing", "legacy_non_user", "legacy_empty"):
            with self.subTest(mode=mode):
                self.fixture(mode)
                with AppServer(str(self.executable)) as server:
                    chat = server.snapshot("thread-a", Settings())
                self.assertEqual(chat.turn_id, "turn-a")
                self.assertIsNone(chat.message_id)
                self.assertEqual(chat.seen_message_ids, frozenset())

    def test_initialization_failure_closes_sdk_process(self):
        self.fixture("initialize_error")
        with self.assertRaisesRegex(CodexUnavailable, "Initialization failed"):
            with AppServer(str(self.executable)):
                self.fail("An uninitialized connection escaped")
        pid = json.loads(self.request_log.read_text().splitlines()[0])["pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_rpc_failure_does_not_break_the_next_request(self):
        self.fixture("request_error")
        with AppServer(str(self.executable)) as server:
            with self.assertRaisesRegex(CodexUnavailable, "Missing thread-a"):
                server.read_chat("thread-a")
            self.assertEqual(server.read_chat("thread-b").id, "thread-b")
