"""Exercise public SDK reads, queue readiness, and desktop-owned dispatch."""

from pathlib import Path
from dataclasses import replace
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import carrie_on

from codex_bridge import CodexClient, BridgeError, OwnerUnavailable
from resume_queue import Entry, Usage, check_queue, locked_queue
from test_carrie_on import usage


class Settings:
    def __init__(self):
        self.delivered = []

    def read_settings(self, thread_id):
        return {"model": "model-a", "resumeState": "resumed"}

    def snapshot(self, *args, **kwargs):
        raise AssertionError("Queue operations must not fetch a desktop snapshot")

    def continue_chat(self, thread_id, prompt, message_id):
        self.delivered.append((thread_id, prompt, message_id))
        return "accepted-turn"


class SDKProtocol(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.executable = Path(directory.name) / "codex"
        self.state = Path(directory.name) / "queue.json"
        self.request_log = Path(directory.name) / "requests.jsonl"

    def fixture(self, mode="normal", *, turn=None):
        self.executable.write_text(f"#!{sys.executable}\n" + '''
import json, sys
mode = ''' + repr(mode) + '''
turn_override = ''' + repr(turn) + '''
request_log = ''' + repr(str(self.request_log)) + '''
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
''')
        self.executable.chmod(0o700)

    def test_latest_header_keeps_an_unfinished_desktop_turn_busy(self):
        self.fixture()
        with CodexClient.connect(str(self.executable)) as server:
            chat = server.read_chat("thread-a", Settings())
        self.assertEqual((chat.turn_id, chat.title, chat.busy, chat.blocked),
                         ("turn-a", "Finish feature", True, False))

    def test_completion_evidence_distinguishes_running_and_ended_work(self):
        cases = (
            ("failed", None, True), ("failed", 0, True), ("failed", 99, False),
            ("completed", None, True), ("completed", 0, True), ("completed", 99, False),
            ("interrupted", None, True), ("interrupted", 0, True), ("interrupted", 99, False),
            ("inProgress", None, True), ("inProgress", 0, True), ("inProgress", 99, True),
        )
        for status, completed_at, busy in cases:
            with self.subTest(status=status, completed_at=completed_at):
                self.fixture(turn={"status": status, "completedAt": completed_at})
                with CodexClient.connect(str(self.executable)) as server:
                    chat = server.read_chat("thread-a", Settings())
                self.assertEqual(chat.busy, busy)

    def test_quota_failed_chat_waits_for_completion_evidence_before_dispatch(self):
        self.fixture("quota", turn={"completedAt": None})
        desktop = Settings()
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            chat = server.read_chat("thread-a", desktop)
            entry = Entry.enroll(chat, Usage.parse(usage()), "Continue", 50)
            queue.entries.append(entry)
            self.assertEqual(check_queue(queue, server, desktop, 110), [])
            self.assertEqual((entry.state, entry.note, desktop.delivered),
                             ("waiting", "Waiting: chat is active or requires attention", []))
        self.fixture("quota")
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            self.assertEqual(check_queue(queue, server, desktop, 110), ["Continuation accepted: Finish feature"])
            self.assertEqual((queue.entries, queue.data["history"][0]["outcome"]), ([], "resumed"))
        self.assertEqual(desktop.delivered, [("thread-a", "Continue", entry.id)])

    def test_failed_initialization_closes_the_sdk_process(self):
        self.fixture("initialize_error")
        with self.assertRaisesRegex(BridgeError, "Initialization failed"):
            with CodexClient.connect(str(self.executable)):
                self.fail("An uninitialized client escaped connect()")
        pid = json.loads(self.request_log.read_text().splitlines()[0])["pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_summaries_preserve_catalog_and_saved_title_fallbacks(self):
        self.fixture("unnamed")
        with CodexClient.connect(str(self.executable)) as server:
            expected = {"id": "thread-a", "title": "Finish feature", "cwd": "/tmp", "last_activity": 2}
            self.assertEqual(server.list_chats(), {"chats": [expected], "next_cursor": None})
            self.assertEqual(server.read_chat_summary("thread-a"), expected)
            self.assertEqual(server.read_chat_summary("thread-a", fallback_title="Saved title"),
                             {**expected, "title": "Saved title"})

    def test_dormant_chat_opens_only_after_catalog_validation(self):
        self.fixture("quota")
        desktop = Settings()
        with CodexClient.connect(str(self.executable)) as server:
            with patch.object(desktop, "read_settings", side_effect=OwnerUnavailable("no-client-found")), \
                    patch.object(desktop, "open_chat", create=True, return_value={"model": "model-a", "resumeState": "resumed"}) as opened:
                with self.assertRaises(OwnerUnavailable):
                    server.read_chat("thread-a", desktop)
                self.assertEqual(opened.call_count, 0)
                with patch.object(server, "contains_chat", return_value=False):
                    with self.assertRaisesRegex(ValueError, "archived"):
                        server.read_chat("thread-a", desktop, open_if_needed=True)
                    self.assertEqual(opened.call_count, 0)
                chat = server.read_chat("thread-a", desktop, open_if_needed=True)
                self.assertEqual((chat.turn_id, chat.busy, chat.blocked), ("turn-a", False, False))
                opened.assert_called_once_with("thread-a")

    def test_owned_chat_does_not_require_dormant_activation_checks(self):
        self.fixture("quota")
        with CodexClient.connect(str(self.executable)) as server:
            with patch.object(server, "contains_chat", side_effect=AssertionError("Owned chat must not be opened")):
                chat = server.read_chat("thread-a", Settings(), open_if_needed=True)
        self.assertEqual((chat.turn_id, chat.model), ("turn-a", "model-a"))

    def test_legacy_entry_migrates_through_sdk_and_continues_once_after_reload(self):
        for mode in ("quota", "legacy_compaction"):
            with self.subTest(mode=mode):
                if self.state.exists():
                    self.state.unlink()
                self.fixture(mode)
                desktop = Settings()
                with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
                    chat = server.read_chat("thread-a", desktop)
                    entry = Entry.enroll(chat, Usage.parse(usage()), "Finish tests", 50)
                    entry.anchor_message_id, entry.anchor_turn_id = "original", None
                    queue.entries.append(entry)
                    queue.save()
                    self.assertEqual(check_queue(queue, server, desktop, 110), ["Continuation accepted: Finish feature"])
                with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
                    self.assertEqual(check_queue(queue, server, desktop, 110), [])
                    self.assertEqual((queue.data["history"][0]["anchor_turn_id"], queue.data["history"][0]["prompt"]),
                                     ("turn-a", "Finish tests"))
                self.assertEqual(desktop.delivered, [("thread-a", "Finish tests", entry.id)])

    def test_legacy_identity_without_evidence_never_sends(self):
        for mode in ("legacy_missing", "legacy_changed", "legacy_wrong_turn", "legacy_non_user",
                     "legacy_empty", "legacy_cursor", "legacy_race"):
            with self.subTest(mode=mode):
                if self.state.exists():
                    self.state.unlink()
                self.fixture(mode)
                desktop = Settings()
                with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
                    chat = server.read_chat("thread-a", desktop)
                    entry = Entry.enroll(chat, Usage.parse(usage()), "Finish tests", 50)
                    entry.anchor_message_id, entry.anchor_turn_id = "original", None
                    queue.entries.append(entry)
                    check_queue(queue, server, desktop, 110)
                    self.assertEqual((entry.state, entry.anchor_turn_id, desktop.delivered),
                                     ("needs_attention", "turn-a" if mode == "legacy_race" else None, []))

    def test_legacy_uncertain_send_without_a_matching_client_id_is_not_retried(self):
        self.fixture("quota")
        desktop = Settings()
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            entry = Entry.enroll(server.read_chat("thread-a", desktop), Usage.parse(usage()), "Finish tests", 50)
            entry.state, entry.attempted_at, entry.anchor_turn_id = "sending", 100, None
            entry.anchor_message_id = "original"
            queue.entries.append(entry)
            check_queue(queue, server, desktop, 110)
            self.assertEqual((entry.state, entry.accepted_turn_id, desktop.delivered), ("needs_attention", None, []))
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            self.assertEqual((check_queue(queue, server, desktop, 110), desktop.delivered), ([], []))

    def test_legacy_lost_receipt_is_confirmed_on_an_earlier_turn_without_resending(self):
        self.fixture("legacy_recovery")
        desktop = Settings()
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            entry = Entry.enroll(server.read_chat("thread-a", desktop), Usage.parse(usage()), "Finish tests", 50)
            # Legacy delivery IDs are enrollment UUIDs. Use the same ID in the SDK fixture.
            self.executable.write_text(self.executable.read_text().replace('"clientId":"original"',
                                                                         '"clientId":' + json.dumps(entry.id)))
            entry.state, entry.attempted_at, entry.anchor_turn_id = "sending", 100, None
            entry.anchor_message_id = "original"
            queue.entries.append(entry)
            queue.save()
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            self.assertEqual(check_queue(queue, server, desktop, 110), ["Confirmed continuation: Finish feature"])
            self.assertEqual((queue.data["history"][0]["accepted_turn_id"], desktop.delivered), ("earlier-turn", []))
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            self.assertEqual((check_queue(queue, server, desktop, 110), len(queue.data["history"])), ([], 1))

    def test_uncaught_sdk_rpc_error_ends_the_session_as_a_bridge_error(self):
        self.fixture("error")
        with self.assertRaisesRegex(BridgeError, "Missing thread"):
            with CodexClient.connect(str(self.executable)) as server:
                server.read_chat("thread-a", Settings())

    def test_chat_rpc_error_does_not_prevent_later_entries_from_continuing(self):
        for mode in ("request_error", "unknown_request_error"):
            with self.subTest(mode=mode):
                if self.state.exists():
                    self.state.unlink()
                self.fixture("quota")
                with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
                    chat = server.read_chat("thread-a", Settings())
                    for thread_id, prompt in (("thread-a", "First"), ("thread-b", "Second")):
                        queue.entries.append(Entry.enroll(replace(chat, thread_id=thread_id), Usage.parse(usage()), prompt, 50))
                    second_id = queue.entries[1].id
                    queue.save()
                self.fixture(mode)
                desktop = Settings()
                with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
                    outcomes = check_queue(queue, server, desktop, 110)
                    self.assertIn("Missing thread-a", outcomes[0])
                    self.assertEqual(outcomes[1], "Continuation accepted: Finish feature")
                with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
                    outcomes = check_queue(queue, server, desktop, 110)
                    self.assertEqual(len(outcomes), 1)
                    self.assertEqual([(entry.thread_id, entry.state) for entry in queue.entries], [("thread-a", "waiting")])
                    self.assertIn("Missing thread-a", queue.entries[0].reactivation_error)
                    self.assertEqual([(record["id"], record["outcome"]) for record in queue.data["history"]],
                                     [(second_id, "resumed")])
                self.assertEqual(desktop.delivered, [("thread-b", "Second", second_id)])

    def test_disconnected_sdk_session_releases_queue_lock_and_preserves_all_entries(self):
        self.fixture()
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            chat = server.read_chat("thread-a", Settings())
            for thread_id, prompt in (("thread-a", "First"), ("thread-b", "Second")):
                queue.entries.append(Entry.enroll(replace(chat, thread_id=thread_id), Usage.parse(usage()), prompt, 50))
            queue.save()
        self.fixture("disconnect")
        self.request_log.write_text("")
        args = carrie_on.parser().parse_args(["--state", str(self.state), "--codex", str(self.executable), "tick"])
        settings = Settings()
        with patch("carrie_on.DesktopClient") as desktop, patch("resume_queue.time.time", return_value=110):
            desktop.return_value.__enter__.return_value = settings
            with self.assertRaises(BridgeError) as raised:
                carrie_on.run(args)
        with locked_queue(self.state) as queue:
            self.assertEqual([(entry.prompt, entry.state) for entry in queue.entries],
                             [("First", "waiting"), ("Second", "waiting")])
            self.assertTrue(all(entry.reactivation_error == str(raised.exception) for entry in queue.entries))
            self.assertEqual(queue.data["history"], [])
            queue.data["paused"] = True
            queue.save()
        self.assertEqual(json.loads(self.state.read_text())["paused"], True)
        reads = [json.loads(line) for line in self.request_log.read_text().splitlines()]
        self.assertEqual([read["params"]["threadId"] for read in reads if read["method"] == "thread/read"], ["thread-a"])
        self.assertEqual(settings.delivered, [])

    def test_catalog_membership_reaches_later_pages(self):
        self.fixture("pages")
        with CodexClient.connect(str(self.executable)) as server:
            self.assertEqual((server.contains_chat("thread-a", "Finish feature"),
                              server.contains_chat("missing", "Finish feature")), (True, False))

    def test_unnamed_and_untitled_chats_continue_using_catalog_identity(self):
        for mode, title in (("unnamed", "Finish feature"), ("untitled", "thread-a")):
            with self.subTest(mode=mode):
                self.fixture(mode)
                desktop = Settings()
                with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
                    entry = Entry.enroll(server.read_chat("thread-a", desktop), Usage.parse(usage()), "Continue", 50)
                    queue.entries.append(entry)
                    queue.save()
                    self.assertEqual((entry.title, entry.anchor_message_id, entry.anchor_turn_id),
                                     (title, None, "turn-a"))
                    self.assertEqual(check_queue(queue, server, desktop, 110), ["Continuation accepted: " + title])
                self.assertEqual(desktop.delivered, [("thread-a", "Continue", entry.id)])

    def test_helper_reads_goals_without_changing_desktop_goal_state(self):
        self.fixture("paused")
        desktop = Settings()
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            entry = Entry.enroll(server.read_chat("thread-a", desktop), Usage.parse(usage()), "Continue", 50)
            queue.entries.append(entry)
            queue.save()
            self.assertEqual(check_queue(queue, server, desktop, 110), [])
            self.assertEqual((entry.state, entry.note), ("waiting", "Waiting: chat is active or requires attention"))
        self.assertEqual(desktop.delivered, [])

    def enroll_running_chat(self, desktop):
        self.fixture()
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            entry = Entry.enroll(server.read_chat("thread-a", desktop), Usage.parse(usage()), "Finish tests", 50)
            queue.entries.append(entry)
            queue.save()
            self.assertEqual(check_queue(queue, server, desktop, 110), [])
            self.assertEqual((entry.state, entry.note), ("waiting", "Waiting: chat is active or requires attention"))
            return entry.id

    def test_running_chat_waits_then_quota_failure_continues_in_desktop_once(self):
        desktop = Settings()
        entry_id = self.enroll_running_chat(desktop)
        self.fixture("quota")
        with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
            self.assertEqual(check_queue(queue, server, desktop, 110), ["Continuation accepted: Finish feature"])
            self.assertEqual(check_queue(queue, server, desktop, 110), [])
        self.assertEqual(desktop.delivered, [("thread-a", "Finish tests", entry_id)])
        self.assertEqual(json.loads(self.state.read_text())["history"][0]["accepted_turn_id"], "accepted-turn")

    def test_finished_stopped_and_changed_work_is_not_continued(self):
        for mode, expected in (("completed", "finished_before_reset"), ("stopped", "needs_attention"),
                               ("changed", "needs_attention")):
            with self.subTest(mode=mode):
                if self.state.exists():
                    self.state.unlink()
                desktop = Settings()
                self.enroll_running_chat(desktop)
                self.fixture(mode)
                with CodexClient.connect(str(self.executable)) as server, locked_queue(self.state) as queue:
                    check_queue(queue, server, desktop, 110)
                state = json.loads(self.state.read_text())
                outcome = state["history"][0]["outcome"] if state["history"] else state["entries"][0]["state"]
                self.assertEqual((outcome, desktop.delivered), (expected, []))


if __name__ == "__main__":
    unittest.main()
