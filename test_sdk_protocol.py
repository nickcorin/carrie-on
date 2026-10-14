"""Exercise SDK subprocess reads, response limits, and persisted completion rules."""

from pathlib import Path
import json
import sys
import tempfile
import unittest

from codex_bridge import AppServer, BridgeError
from resume_queue import Chat, Entry, Usage, check_queue, locked_queue
from test_carrie_on import usage


class Settings:
    def __init__(self):
        self.delivered = []

    def settings(self, thread_id):
        return {"model": "model-a", "resumeState": "resumed"}

    def snapshot(self, *args, **kwargs):
        raise AssertionError("New enrollments must not fetch a desktop snapshot")

    def continue_chat(self, thread_id, prompt, message_id):
        self.delivered.append((thread_id, prompt, message_id))
        return {"turn_id": "accepted-turn"}


class SDKProtocol(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.executable = Path(directory.name) / "codex"
        self.state = Path(directory.name) / "queue.json"

    def fixture(self, mode="normal"):
        self.executable.write_text(f"#!{sys.executable}\n" + '''
import json, sys, time
mode = ''' + repr(mode) + '''
thread = {"id":"thread-a", "name":"Finish feature", "preview":"", "modelProvider":"openai",
          "createdAt":1,"updatedAt":2,"cwd":"/tmp","cliVersion":"0.162.1","ephemeral":False,
          "sessionId":"session-a","source":"appServer","status":{"type":"notLoaded"},"turns":[]}
for line in sys.stdin:
    message = json.loads(line)
    method = message["method"]
    if method == "initialized": continue
    if method == "initialize": result = {"userAgent":"codex/0.162.1"}
    elif mode == "oversized":
        sys.stdout.write("x" * (1024 * 1024 + 1)); sys.stdout.flush(); time.sleep(10); continue
    elif mode == "timeout": time.sleep(10); continue
    elif mode == "error":
        print(json.dumps({"id":message["id"],"error":{"code":-32602,"message":"Missing thread"}}),flush=True)
        continue
    elif method == "thread/read":
        assert message["params"]["includeTurns"] is False
        result = {"thread":thread}
    elif method == "thread/turns/list":
        assert message["params"] == {"threadId":"thread-a","limit":1,"sortDirection":"desc","itemsView":"notLoaded"}
        turn = {"id":"turn-a","status":"interrupted","completedAt":None,"items":[],"itemsView":"notLoaded"}
        if mode == "quota": turn.update(status="failed",completedAt=99,error={"message":"Quota exhausted","codexErrorInfo":"usageLimitExceeded"})
        if mode == "completed": turn.update(status="completed",completedAt=99)
        if mode == "stopped": turn.update(completedAt=99)
        if mode == "changed": turn.update(id="turn-b",status="completed",completedAt=99)
        result = {"data":[turn]}
    elif method == "thread/goal/get": result = {"goal":None}
    elif method == "thread/list": result = {"data":[thread],"nextCursor":None}
    elif method == "account/rateLimits/read":
        bucket = {"limitId":"codex","primary":{"usedPercent":0,"windowDurationMins":300,"resetsAt":200}}
        result = {"accountId":"account-a","ordinaryUsageAllowed":True,"rateLimits":bucket,"rateLimitsByLimitId":{"codex":bucket}}
    else: raise RuntimeError("Unexpected request: " + method)
    print(json.dumps({"id":message["id"],"result":result}),flush=True)
''')
        self.executable.chmod(0o700)

    def test_latest_header_keeps_an_unfinished_desktop_turn_busy(self):
        self.fixture()
        with AppServer(str(self.executable), timeout=2) as server:
            chat = server.read_chat("thread-a", Settings())
        self.assertEqual((chat.turn_id, chat.title, chat.busy, chat.blocked),
                         ("turn-a", "Finish feature", True, False))

    def test_oversized_unterminated_response_trips_before_json_decoding(self):
        self.fixture("oversized")
        with AppServer(str(self.executable), timeout=2) as server:
            with self.assertRaisesRegex(BridgeError, "exceeded 1 MiB"):
                server.read_chat("thread-a", Settings())

    def test_silent_process_is_stopped_at_the_request_deadline(self):
        self.fixture("timeout")
        with AppServer(str(self.executable), timeout=2) as server:
            server.client.timeout = 0.2
            with self.assertRaisesRegex(BridgeError, "timed out"):
                server.read_chat("thread-a", Settings())

    def test_sdk_rpc_error_becomes_a_recoverable_bridge_error(self):
        self.fixture("error")
        with AppServer(str(self.executable), timeout=2) as server:
            with self.assertRaisesRegex(BridgeError, "Missing thread"):
                server.read_chat("thread-a", Settings())

    def enroll_running_chat(self, desktop):
        self.fixture()
        with AppServer(str(self.executable), timeout=2) as server, locked_queue(self.state) as queue:
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
        with AppServer(str(self.executable), timeout=2) as server, locked_queue(self.state) as queue:
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
                with AppServer(str(self.executable), timeout=2) as server, locked_queue(self.state) as queue:
                    check_queue(queue, server, desktop, 110)
                state = json.loads(self.state.read_text())
                outcome = state["history"][0]["outcome"] if state["history"] else state["entries"][0]["state"]
                self.assertEqual((outcome, desktop.delivered), (expected, []))


class CompletionEvidence(unittest.TestCase):
    def chat(self, status="failed", completed=100, error="usageLimitExceeded", goal=None):
        return Chat.from_headers({"id":"thread-a", "name":"Finish feature"}, [{
            "id":"turn-a", "status":status, "completedAt":completed,
            "error":{"codexErrorInfo":error},
        }], Settings().settings("thread-a"), goal)

    def test_completion_evidence_distinguishes_running_and_ended_work(self):
        for status in ("failed", "completed", "interrupted"):
            with self.subTest(status=status):
                self.assertEqual((self.chat(status, None).busy, self.chat(status, 100).busy), (True, False))
        self.assertEqual((self.chat().status, self.chat().quota_failed), ("failed", True))
        self.assertEqual((self.chat("interrupted", 100, None).busy,
                          self.chat("interrupted", 100, None).quota_failed), (False, False))

    def test_paused_goals_block_completed_turns(self):
        self.assertEqual((self.chat(goal={"status":"paused"}).blocked,
                          self.chat(goal={"status":"active"}).blocked), (True, False))


if __name__ == "__main__":
    unittest.main()
