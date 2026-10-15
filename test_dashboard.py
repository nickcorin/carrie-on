"""Dashboard HTTP boundary and real queue behavior with model-free fixtures."""

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace

from openai_codex.errors import JsonRpcError

from test_carrie_on import chat, usage
from carrie_on_ui import Dashboard, DashboardServer
from resume_queue import Usage, UsageObservation, locked_queue


CHAT_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


class LocalCodex:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    @classmethod
    def connect(cls, *args, **kwargs):
        return cls(*args, **kwargs)

    def read_usage(self):
        return usage(25, int(time.time()) + 3600)

    def list_chats(self, search=None, *, cursor=None, limit=25):
        title = 'A chat with <script> & "quotes"'
        chats = [] if search and search not in title else [
            {"id": CHAT_ID, "title": title, "cwd": "/projects/example", "last_activity": 1791900000}]
        return {"chats": chats, "next_cursor": None}

    def read_chat_summary(self, thread_id, *, fallback_title=None):
        if thread_id != CHAT_ID:
            raise ValueError("Unknown fixture chat")
        return self.list_chats()["chats"][0]

    def contains_chat(self, thread_id, title):
        return thread_id == CHAT_ID

    def read_chat(self, thread_id, _desktop, *, include_goal=False, open_if_needed=False):
        return replace(chat("completed"), thread_id=thread_id, title=self.list_chats()["chats"][0]["title"])


class DashboardBehavior(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "queue.json"
        for target in ("CodexClient", "DesktopClient"):
            mock = patch("carrie_on." + target, LocalCodex)
            mock.start()
            self.addCleanup(mock.stop)
        service = patch("carrie_on.launch_agent", return_value={"installed": True})
        service.start()
        self.addCleanup(service.stop)
        self.server = DashboardServer(Dashboard(self.state, "codex"), port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        data = raw if raw is not None else json.dumps(body) if body is not None else None
        request_headers = {"X-Carrie-On": "1", "Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection.request("POST" if data is not None else "GET", path, data, request_headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, json.loads(payload) if response.headers.get_content_type() == "application/json" else payload

    def test_setup_health_reports_running_build_without_contacting_codex(self):
        from app_runtime import VERSION
        self.server.dashboard.build_id = "running-build"
        with patch.object(self.server.dashboard, "command", side_effect=AssertionError("Health contacted Codex")):
            status, health = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health, {"version": VERSION, "build_id": "running-build"})
        self.assertEqual(self.request("/api/health", headers={"X-Carrie-On": ""})[0], 403)

    def test_connection_reports_handshake_and_recovers_without_changing_queue(self):
        self.request("/api/queue", {"chat": CHAT_ID, "prompt": "Continue the tests."})
        self.assertEqual(self.request("/api/connection"), (200, {"connected": True}))
        with patch("carrie_on.DesktopClient", side_effect=FileNotFoundError("No socket")):
            self.assertEqual(self.request("/api/connection"), (200, {"connected": False}))
            _, queue = self.request("/api/status")
            self.assertEqual(queue["entries"][0]["prompt"], "Continue the tests.")
        self.assertEqual(self.request("/api/connection"), (200, {"connected": True}))
        self.assertEqual(self.request("/api/connection", headers={"X-Carrie-On": ""})[0], 403)

    def test_select_queue_duplicate_pause_resume_remove_and_history(self):
        status, chats = self.request("/api/chats")
        self.assertEqual(status, 200)
        self.assertEqual(chats[0]["title"], 'A chat with <script> & "quotes"')
        status, result = self.request("/api/queue", {"chat": chats[0]["id"], "prompt": "--finish tests"})
        self.assertEqual((status, result["paused"]), (200, False))
        status, error = self.request("/api/queue", {"chat": CHAT_ID})
        self.assertEqual(status, 400)
        self.assertIn("already queued", error["error"])
        _, result = self.request("/api/status")
        self.assertEqual(result["entries"][0]["prompt"], "--finish tests")
        self.assertEqual(result["entries"][0]["state"], "waiting")
        enrollment_id = result["entries"][0]["id"]
        self.request("/api/pause", {})
        self.assertEqual(self.request("/api/status")[1]["paused"], True)
        self.request("/api/resume", {})
        self.assertEqual(self.request("/api/status")[1]["paused"], False)
        self.request("/api/remove", {"chat": enrollment_id})
        _, result = self.request("/api/status")
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["history"][0]["outcome"], "cancelled")

    def test_cross_origin_rebinding_and_missing_header_cannot_mutate(self):
        for headers in ({"Origin": "https://evil.example"}, {"Host": "evil.example"},
                        {"X-Carrie-On": ""}, {"Sec-Fetch-Site": "cross-site"}):
            status, _ = self.request("/api/pause", {}, headers)
            self.assertEqual(status, 403)
        self.assertEqual(self.request("/api/status")[1]["paused"], False)
        self.assertEqual(self.request("/api/pause", {})[1]["queued"], 0)
        self.assertEqual(self.request("/api/status")[1]["paused"], True)

    def test_edit_instruction_preserves_enrollment_and_retries_without_model_access(self):
        _, queued = self.request("/api/queue", {"chat": CHAT_ID, "prompt": "Original instruction"})
        entry = queued["entry"]
        body = {"chat": entry["id"], "prompt": "--finish the revised tests", "expected_prompt": entry["prompt"]}
        with patch("carrie_on.CodexClient", side_effect=AssertionError("Editing must not contact Codex")):
            for _ in range(2):
                status, edited = self.request("/api/instruction", body)
                self.assertEqual(status, 200)
                self.assertEqual(edited["entry"], {**entry, "prompt": "--finish the revised tests"})
        self.assertEqual(self.request("/api/status")[1]["entries"][0]["prompt"], "--finish the revised tests")

    def test_activity_metadata_for_catalog_and_queued_chats(self):
        self.assertEqual(self.request("/api/queued-chats"), (200, []))
        status, chats = self.request("/api/chats")
        self.assertEqual(status, 200)
        self.assertEqual(chats[0]["last_activity"], 1791900000)
        self.request("/api/queue", {"chat": CHAT_ID, "prompt": "Continue"})
        status, queued = self.request("/api/queued-chats")
        self.assertEqual(status, 200)
        self.assertEqual(queued[0], chats[0])
        self.assertEqual(self.request("/api/chats?search=unmatched")[1], [])
        self.assertEqual(self.request("/api/queued-chats")[1][0]["last_activity"], 1791900000)
        for error in (ValueError("Chat unavailable"), JsonRpcError(12345, "Chat unavailable")):
            with self.subTest(error=type(error).__name__):
                with patch.object(LocalCodex, "read_chat_summary", side_effect=error):
                    status, fallback = self.request("/api/queued-chats")
                self.assertEqual((status, fallback),
                                 (200, [{"id": CHAT_ID, "title": chats[0]["title"], "cwd": None, "last_activity": None}]))

    def test_chat_pages_reach_older_results_and_preserve_search(self):
        class PagedCodex(LocalCodex):
            def list_chats(self, search=None, *, cursor=None, limit=25):
                chats = [{"id": str(index), "title": f"Chat {index}", "last_activity": index, "cwd": None}
                         for index in range(125)]
                if search:
                    chats = [chat for chat in chats if search in chat["title"]]
                chats.sort(key=lambda chat: chat["last_activity"], reverse=True)
                start = int((cursor or "offset:0").split(":")[1])
                end = start + limit
                return {"chats": chats[start:end], "next_cursor": f"offset:{end}" if end < len(chats) else None}

        with patch("carrie_on.CodexClient", PagedCodex):
            status, page = self.request("/api/chat-page")
            self.assertEqual(status, 200)
            self.assertEqual([chat["id"] for chat in page["chats"]], [str(i) for i in range(124, 99, -1)])
            self.assertEqual(page["chats"][0], {"id": "124", "title": "Chat 124", "cwd": None, "last_activity": 124})
            ids = [chat["id"] for chat in page["chats"]]
            while page["next_cursor"]:
                status, page = self.request("/api/chat-page?cursor=" + page["next_cursor"])
                self.assertEqual(status, 200)
                ids.extend(chat["id"] for chat in page["chats"])
            self.assertEqual(ids, [str(i) for i in range(124, -1, -1)])
            status, page = self.request("/api/chat-page?search=Chat%201")
            self.assertEqual(status, 200)
            self.assertEqual([chat["id"] for chat in page["chats"]], [str(i) for i in range(124, 99, -1)])
            status, page = self.request("/api/chat-page?search=Chat%201&cursor=" + page["next_cursor"])
            self.assertEqual([chat["id"] for chat in page["chats"]], [str(i) for i in range(19, 9, -1)] + ["1"])
            self.assertIsNone(page["next_cursor"])
            self.assertEqual(self.request("/api/chat-page?search=absent"), (200, {"chats": [], "next_cursor": None}))
        self.assertEqual(self.request("/api/chat-page", headers={"X-Carrie-On": ""})[0], 403)

    def test_chat_page_errors_are_retryable_and_validate_inputs(self):
        with patch.object(LocalCodex, "list_chats", create=True, side_effect=RuntimeError("Temporarily unavailable")):
            self.assertEqual(self.request("/api/chat-page?cursor=page2"), (503, {"error": "Temporarily unavailable"}))
        with patch.object(LocalCodex, "list_chats", create=True,
                          return_value={"chats": [{"id": "older", "title": "Older chat", "cwd": None,
                                                  "last_activity": None}], "next_cursor": None}):
            self.assertEqual(self.request("/api/chat-page?cursor=page2"), (200, {
                "chats": [{"id": "older", "title": "Older chat", "cwd": None, "last_activity": None}], "next_cursor": None}))
        for query in ("search=" + "x" * 501, "cursor=" + "x" * 16385, "cursor=a&cursor=b", "limit=1000"):
            self.assertEqual(self.request("/api/chat-page?" + query)[0], 503)

    def test_edit_rejects_stale_prompt_or_replaced_enrollment(self):
        _, queued = self.request("/api/queue", {"chat": CHAT_ID, "prompt": "Original"})
        body = {"chat": queued["entry"]["id"], "prompt": "Changed", "expected_prompt": "Original"}
        self.assertEqual(self.request("/api/instruction", body)[0], 200)
        self.assertEqual(self.request("/api/instruction", {**body, "prompt": "Stale overwrite"})[0], 400)
        self.assertEqual(self.request("/api/status")[1]["entries"][0]["prompt"], "Changed")
        self.request("/api/remove", {"chat": body["chat"]})
        self.request("/api/queue", {"chat": CHAT_ID, "prompt": "New enrollment"})
        self.assertEqual(self.request("/api/instruction", body)[0], 400)
        self.assertEqual(self.request("/api/remove", {"chat": body["chat"]})[0], 400)
        self.assertEqual(self.request("/api/status")[1]["entries"][0]["prompt"], "New enrollment")

    def test_edit_rejects_uncertain_sends_invalid_payloads_and_foreign_origins(self):
        _, queued = self.request("/api/queue", {"chat": CHAT_ID, "prompt": "Original"})
        body = {"chat": queued["entry"]["id"], "prompt": "Changed", "expected_prompt": "Original"}
        for change in ({"prompt": ""}, {"prompt": "x" * 32001}, {"prompt": None},
                       {"expected_prompt": None}, {"state": "waiting"}, {"chat": CHAT_ID}):
            self.assertEqual(self.request("/api/instruction", {**body, **change})[0], 400)
        self.assertEqual(self.request("/api/instruction", body, {"Origin": "https://evil.example"})[0], 403)
        for state in ("sending", "needs_attention"):
            with locked_queue(self.state) as queue:
                queue.entries[0].state = state
                queue.entries[0].attempted_at = time.time()
                queue.save()
            self.assertEqual(self.request("/api/instruction", body)[0], 400)
            self.assertEqual(self.request("/api/status")[1]["entries"][0]["prompt"], "Original")

    def test_invalid_input_and_arbitrary_command_rejected(self):
        for path, body in (("/api/queue", {"chat": "--help"}),
                           ("/api/queue", {"chat": CHAT_ID, "prompt": []}),
                           ("/api/queue", {"chat": CHAT_ID, "limit_id": {}}),
                           ("/api/tick", {}), ("/api/pause", [])):
            self.assertEqual(self.request(path, body)[0], 400)
        self.assertEqual(self.request("/api/queue", raw="{")[0], 400)
        self.assertEqual(self.request("/api/queue", {"chat": CHAT_ID})[0], 200)

    def test_native_api_has_no_browser_view(self):
        self.assertEqual(self.request("/resume-queue.json")[0], 404)
        self.assertEqual(self.request("/../resume-queue.json")[0], 404)
        for path in ("/", "/app.js", "/style.css", "/app-icon.png"):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/chats?search=absent")[1], [])
        self.assertEqual(self.request("/api/chats?search=quotes")[1][0]["id"], CHAT_ID)
        self.assertEqual(self.request("/api/usage")[1]["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"], 25)

    def test_menu_summary_reports_usage_and_queue_without_chat_contents(self):
        self.request("/api/queue", {"chat": CHAT_ID, "prompt": "Private continuation instruction"})
        status, summary = self.request("/api/menu")
        self.assertEqual(status, 200)
        self.assertTrue(summary["connected"])
        self.assertEqual(summary["queue"]["count"], 1)
        self.assertEqual(summary["queue"]["paused"], False)
        self.assertEqual(summary["usage"]["windows"][0]["used_percent"], 25)
        self.assertNotIn("Private continuation", json.dumps(summary))
        self.assertNotIn(CHAT_ID, json.dumps(summary))
        self.request("/api/pause", {})
        self.assertEqual(self.request("/api/menu")[1]["queue"]["paused"], True)

    def test_menu_usage_failure_preserves_queue_count(self):
        self.request("/api/queue", {"chat": CHAT_ID})
        with patch.object(LocalCodex, "read_usage", side_effect=RuntimeError("Offline")):
            status, summary = self.request("/api/menu")
        self.assertEqual(status, 200)
        self.assertEqual(summary["queue"]["count"], 1)
        self.assertEqual(summary["usage"], None)
        self.assertEqual(summary["usage_error"], "Offline")

    def test_menu_snapshot_reports_desktop_loss_and_recovery(self):
        with patch("carrie_on.DesktopClient", side_effect=FileNotFoundError("No socket")):
            status, summary = self.request("/api/menu")
        self.assertEqual(status, 200)
        self.assertFalse(summary["connected"])
        self.assertIsNotNone(summary["usage"])
        self.assertTrue(self.request("/api/menu")[1]["connected"])

    def test_recent_usage_enrolls_without_refetching_quota(self):
        self.assertEqual(self.request("/api/menu")[0], 200)
        with patch.object(LocalCodex, "read_usage", side_effect=AssertionError("Cached enrollment must not refetch quota")):
            status, result = self.request("/api/queue", {"chat": CHAT_ID, "prompt": "Continue tests"})
        self.assertEqual(status, 200)
        entry = result["entry"]
        self.assertEqual((entry["thread_id"], entry["anchor_message_id"], entry["anchor_turn_id"],
                          entry["account_id"], entry["prompt"]),
                         (CHAT_ID, None, "turn-original", "account-a", "Continue tests"))
        self.assertEqual(self.request("/api/status")[1]["entries"][0], entry)

    def test_expired_usage_and_elapsed_reset_are_refetched(self):
        now = time.time()
        for observed_at, reset in ((now - 61, int(now) + 3600), (now, int(now) - 1)):
            self.server.dashboard.usage_observations = {
                "codex": UsageObservation(Usage.parse(usage(100, reset, account="old-account")), observed_at)
            }
            status, result = self.request("/api/queue", {"chat": CHAT_ID})
            self.assertEqual(status, 200)
            self.assertEqual(result["entry"]["account_id"], "account-a")
            self.assertGreater(result["entry"]["reset_windows"]["primary"], now)
            self.request("/api/remove", {"chat": result["entry"]["id"]})

    def test_failed_usage_read_invalidates_cached_enrollment_data(self):
        self.request("/api/menu")
        with patch.object(LocalCodex, "read_usage", side_effect=RuntimeError("Offline")):
            self.assertEqual(self.request("/api/menu")[1]["usage_error"], "Offline")
            status, result = self.request("/api/queue", {"chat": CHAT_ID})
            self.assertEqual(status, 503)
            self.assertEqual(result["error"], "Offline")
        self.assertEqual(self.request("/api/queue", {"chat": CHAT_ID})[1]["entry"]["state"], "waiting")

    def test_multiple_cached_buckets_still_require_a_selection(self):
        raw = usage(25, int(time.time()) + 3600)
        raw["rateLimitsByLimitId"]["other"] = raw["rateLimitsByLimitId"]["codex"]
        with patch.object(LocalCodex, "read_usage", return_value=raw):
            self.assertEqual(self.request("/api/menu")[0], 200)
            status, result = self.request("/api/queue", {"chat": CHAT_ID})
            self.assertEqual(status, 400)
            self.assertIn("Choose --limit-id", result["error"])
            with patch.object(LocalCodex, "read_usage", side_effect=AssertionError("Selected cached bucket should suffice")):
                status, result = self.request("/api/queue", {"chat": CHAT_ID, "limit_id": "other"})
            self.assertEqual(status, 200)
            self.assertEqual(result["entry"]["limit_id"], "other")

    def test_late_usage_response_does_not_replace_newer_account_observation(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def delayed_usage(client):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Fixture timed out")
            return usage(25, int(time.time()) + 3600, account="old-account")

        with patch.object(LocalCodex, "read_usage", delayed_usage):
            worker = threading.Thread(target=lambda: results.append(self.request("/api/usage")))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with patch.object(LocalCodex, "read_usage", return_value=usage(25, int(time.time()) + 3600)):
                    self.assertEqual(self.request("/api/usage")[0], 200)
            finally:
                release.set()
                worker.join(5)
        self.assertEqual(results[0][1]["accountId"], "old-account")
        with patch.object(LocalCodex, "read_usage", side_effect=AssertionError("Use the newer cached account")):
            status, result = self.request("/api/queue", {"chat": CHAT_ID})
        self.assertEqual(status, 200)
        self.assertEqual(result["entry"]["account_id"], "account-a")

    def test_slow_enrollment_does_not_lock_other_queue_actions(self):
        entered, release = threading.Event(), threading.Event()
        original_read = LocalCodex.read_chat
        results = []

        def slow_read(client, thread_id, desktop, **kwargs):
            entered.set()
            if not release.wait(8):
                raise RuntimeError("Fixture timed out")
            return original_read(client, thread_id, desktop, **kwargs)

        with patch.object(LocalCodex, "read_chat", slow_read):
            worker = threading.Thread(target=lambda: results.append(self.request("/api/queue", {"chat": CHAT_ID})))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                status, result = self.request("/api/pause", {})
                self.assertEqual((status, result.get("message")), (200, "Monitor paused."))
                self.assertTrue(self.request("/api/status")[1]["paused"])
            finally:
                release.set()
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual((results[0][0], results[0][1]["paused"]), (200, True))

    def test_concurrent_enrollments_cannot_duplicate_a_chat(self):
        barrier = threading.Barrier(2)
        original_read = LocalCodex.read_chat
        results = []

        def simultaneous_read(client, thread_id, desktop, **kwargs):
            barrier.wait(timeout=3)
            return original_read(client, thread_id, desktop, **kwargs)

        with patch.object(LocalCodex, "read_chat", simultaneous_read):
            workers = [threading.Thread(target=lambda: results.append(
                self.request("/api/queue", {"chat": CHAT_ID}))) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
        self.assertEqual(sorted(status for status, _ in results), [200, 400])
        self.assertEqual(len(self.request("/api/status")[1]["entries"]), 1)


if __name__ == "__main__":
    unittest.main()
