"""Test HTTP queue operations against isolated application state."""

import base64
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from carrie_on.codex.client import CodexConnections
from carrie_on.codex.models import (
    ChatPage,
    ChatSummary,
    CodexProtocolError,
    CodexUnavailable,
    UsageSnapshot,
)
from carrie_on.dispatch import DispatchWorker
from carrie_on.server import create_application
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance
from tests.fixtures import CHAT_ID, LocalCodex, usage


class ApiBehavior(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "queue.sqlite3"
        self.store = SqliteQueueStorage(self.path)
        self.connections = CodexConnections("fixture")
        self.app = create_application(
            self.store,
            self.connections,
            maintenance=SqliteStorageMaintenance(self.path),
            run_worker=False,
        )
        self.service = self.app.state.service
        for name in ("AppServer", "Desktop"):
            mock = patch("carrie_on.codex.client." + name, LocalCodex)
            mock.start()
            self.addCleanup(mock.stop)
        self.client = TestClient(
            self.app, base_url="http://127.0.0.1:8766", headers={"X-Carrie-On": "1"}
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def get(self, path, **kwargs):
        return self.client.get("/api/v1/" + path, **kwargs)

    def add(self, **changes):
        body = {"id": str(uuid4()), "chat_id": CHAT_ID, "instruction": "Continue tests", **changes}
        return self.client.post("/api/v1/queue", json=body)

    def cancel(self, item_id):
        return self.client.post(f"/api/v1/queue/{item_id}/cancel", json={})

    def edit(self, item_id, text, revision):
        return self.client.patch(
            f"/api/v1/queue/{item_id}",
            json={"instruction": text, "revision": revision},
        )

    def test_health_openapi_and_absent_pause_or_legacy_api(self):
        with patch.object(
            self.service, "read_usage", side_effect=AssertionError("Health contacted Codex")
        ):
            result = self.get("health")
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("schema_version", result.json())
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/api/v1/queue", schema["paths"])
        self.assertIn(
            "application/json",
            schema["paths"]["/api/v1/queue/{item_id}"]["patch"]["requestBody"]["content"],
        )
        for path in ("pause", "resume", "queue-settings", "tick"):
            self.assertEqual(self.client.post("/api/v1/" + path, json={}).status_code, 404)
        self.assertEqual(self.client.get("/api/status").status_code, 404)

    def test_create_list_cancel_history_and_response_headers(self):
        response = self.add(instruction="--finish tests")
        self.assertEqual(response.status_code, 201, response.text)
        item = response.json()
        self.assertEqual(response.headers["Location"], f"/api/v1/queue/{item['id']}")
        self.assertEqual(item["revision"], 1)
        self.assertNotIn("ETag", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(item["queued_at"].endswith("Z"))
        self.assertEqual(self.get("queue").json()["items"][0]["instruction"], "--finish tests")
        self.assertEqual(self.cancel(item["id"]).status_code, 200)
        self.assertEqual(self.cancel(item["id"]).status_code, 200)
        self.assertEqual(self.get("queue").json()["items"], [])
        self.assertEqual(self.get("queue?state=all").json()["items"][0]["state"], "cancelled")

    def test_chat_resolution_accepts_ids_links_and_exact_unique_titles(self):
        title = LocalCodex().threads()[0].title
        for query in (CHAT_ID.upper(), f"codex://threads/{CHAT_ID}", title):
            response = self.get("chats/resolve", params={"query": query})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["id"], CHAT_ID)
        self.assertEqual(self.get("chats/resolve", params={"query": title[:6]}).status_code, 422)
        with patch.object(
            LocalCodex,
            "threads",
            return_value=[
                ChatSummary(id=CHAT_ID, title=title, cwd=None, last_activity=None),
                ChatSummary(id=str(uuid4()), title=title, cwd=None, last_activity=None),
            ],
        ):
            response = self.get("chats/resolve", params={"query": title})
        self.assertEqual(response.status_code, 422)
        self.assertIn("2 exact title matches", response.json()["detail"])

    def test_idempotent_creation_survives_edit_and_cancel_without_codex_access(self):
        item_id = str(uuid4())
        item = self.add(id=item_id).json()
        self.assertEqual(self.edit(item_id, "Changed", item["revision"]).status_code, 200)
        self.cancel(item_id)
        restarted = create_application(
            SqliteQueueStorage(self.path),
            self.connections,
            maintenance=SqliteStorageMaintenance(self.path),
            run_worker=False,
        )
        with (
            TestClient(
                restarted, base_url="http://127.0.0.1:8766", headers={"X-Carrie-On": "1"}
            ) as client,
            patch(
                "carrie_on.codex.client.Desktop",
                side_effect=AssertionError("Replay contacted Codex"),
            ),
        ):
            response = client.post(
                "/api/v1/queue",
                json={"id": item_id, "chat_id": CHAT_ID, "instruction": "Continue tests"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.json()["instruction"], response.json()["state"]), ("Changed", "cancelled")
        )
        replay = self.add(id=item_id, instruction="Different input", limit_id="different")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["instruction"], "Changed")
        self.assertEqual(self.add(id=item_id, chat_id=str(uuid4())).status_code, 409)

    def test_duplicate_chat_and_stale_item_ids_cannot_change_replacement(self):
        first = self.add().json()
        self.assertEqual(self.add().status_code, 409)
        self.cancel(first["id"])
        second = self.add(instruction="Replacement").json()
        self.assertEqual(self.edit(first["id"], "Stale edit", first["revision"]).status_code, 409)
        self.cancel(first["id"])
        self.assertEqual(self.get("queue").json()["items"][0]["id"], second["id"])

    def test_edits_preserve_anchors_and_require_current_revision(self):
        item = self.add().json()
        with patch(
            "carrie_on.codex.client.AppServer", side_effect=AssertionError("Edit contacted Codex")
        ):
            response = self.edit(item["id"], "--new instruction", 1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["instruction"], "--new instruction")
        self.assertEqual(response.json()["revision"], 2)
        self.assertNotIn("ETag", response.headers)
        for key in ("id", "chat_id", "queued_at", "anchor_message_id", "model", "reset_windows"):
            self.assertEqual(response.json()[key], item[key])
        stale = self.edit(item["id"], "Overwrite", 1)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.headers["content-type"], "application/problem+json")
        self.assertEqual(stale.json()["code"], "stale_revision")
        missing = self.client.patch(
            f"/api/v1/queue/{item['id']}",
            json={"instruction": "x"},
            headers={"If-Match": '"revision-2"'},
        )
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(missing.json()["code"], "invalid_request")
        saved = self.get(f"queue/{item['id']}")
        self.assertEqual(saved.json()["instruction"], "--new instruction")
        self.assertEqual(saved.json()["revision"], 2)
        self.assertNotIn("ETag", saved.headers)

    def test_concurrent_edits_with_same_revision_only_save_one_instruction(self):
        item = self.add().json()
        barrier = threading.Barrier(2)

        def edit(text):
            barrier.wait(timeout=5)
            return self.edit(item["id"], text, item["revision"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(edit, ("First edit", "Second edit")))
        self.assertCountEqual([response.status_code for response in responses], [200, 409])
        saved = self.get(f"queue/{item['id']}").json()
        winner = next(response.json() for response in responses if response.status_code == 200)
        conflict = next(response.json() for response in responses if response.status_code == 409)
        self.assertEqual(conflict["code"], "stale_revision")
        self.assertEqual(saved["instruction"], winner["instruction"])
        self.assertEqual(saved["revision"], 2)

    def test_active_delivery_blocks_edit_cancel_but_not_reads(self):
        item = self.add().json()
        claimed = self.store.create_delivery_attempt().entry
        response = self.edit(item["id"], "Change", claimed.revision)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "delivery_active")
        self.assertEqual(self.cancel(item["id"]).status_code, 409)
        self.assertEqual(self.get("queue").status_code, 200)
        self.assertTrue(self.get(f"queue/{item['id']}").json()["dispatching"])
        self.assertEqual(self.get(f"queue/{item['id']}").json()["instruction"], "Continue tests")
        self.assertEqual(self.get(f"queue/{item['id']}").json()["revision"], item["revision"])
        self.assertEqual(self.get(f"queue/{item['id']}/deliveries").status_code, 404)

    def test_local_security_and_no_arbitrary_file_or_command_access(self):
        for headers in (
            {"Origin": "https://evil.example"},
            {"Host": "evil.example"},
            {"X-Carrie-On": ""},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            response = self.client.post("/api/v1/queue", json={}, headers=headers)
            self.assertEqual(response.status_code, 403)
        for path in ("/", "/queue.sqlite3", "/resume-queue.json", "/app.js"):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.get("queue").json()["items"], [])

    def test_invalid_payloads_and_protected_fields_rejected(self):
        for change in (
            {"instruction": ""},
            {"instruction": " "},
            {"instruction": None},
            {"instruction": "x" * 32001},
            {"instruction": []},
            {"chat_id": "--help"},
            {"limit_id": {}},
            {"state": "sent"},
            {"deferrals": 0},
        ):
            with self.subTest(change=list(change)):
                self.assertEqual(self.add(**change).status_code, 422)
        self.assertEqual(
            self.client.post(
                "/api/v1/queue", content="{", headers={"Content-Type": "application/json"}
            ).status_code,
            422,
        )
        self.assertEqual(self.client.post("/api/v1/queue", content="x").status_code, 415)
        self.assertEqual(
            self.client.post(
                "/api/v1/queue", content="x" * 150001, headers={"Content-Type": "application/json"}
            ).status_code,
            413,
        )
        for query in (
            "limit=101",
            "limit=0",
            "cursor=a&cursor=b",
            "unknown=1",
            "search=" + "x" * 501,
        ):
            self.assertEqual(self.get("chats?" + query).status_code, 422)

    def test_menu_summary_has_no_chat_content_and_partial_usage_failure_keeps_queue(self):
        self.add(instruction="Private instruction")
        response = self.get("status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["queue"]["count"], 1)
        self.assertNotIn("Private instruction", response.text)
        self.assertNotIn(CHAT_ID, response.text)
        self.assertNotIn("paused", response.text)
        with patch.object(LocalCodex, "usage", side_effect=CodexUnavailable("Offline")):
            value = self.get("status").json()
        self.assertEqual(value["queue"]["count"], 1)
        self.assertIsNone(value["usage"])
        self.assertEqual(value["usage_error"], "Offline")

    def test_offline_queue_management_connection_loss_and_recovery(self):
        item = self.add().json()
        self.assertTrue(self.get("connection").json()["connected"])
        with patch("carrie_on.codex.client.Desktop", side_effect=CodexUnavailable("Closed")):
            self.assertFalse(self.get("connection").json()["connected"])
            self.assertEqual(self.edit(item["id"], "Offline edit", 1).status_code, 200)
            self.assertEqual(self.cancel(item["id"]).status_code, 200)
            self.assertEqual(self.add().status_code, 503)
        self.assertTrue(self.get("connection").json()["connected"])

    def test_activity_metadata_is_independent_of_search_and_falls_back(self):
        self.assertEqual(self.get("queue/metadata").json()["items"], [])
        catalog = self.get("chats").json()["items"]
        self.add()
        self.assertEqual(self.get("queue/metadata").json()["items"], catalog)
        self.assertEqual(self.get("chats?search=absent").json()["items"], [])
        with patch.object(LocalCodex, "read_chat", side_effect=CodexUnavailable("Absent")):
            fallback = self.get("queue/metadata").json()["items"][0]
        self.assertEqual(fallback["title"], catalog[0]["title"])
        self.assertIsNone(fallback["last_activity"])

    def test_chat_pagination_beyond_100_search_and_cursor_scope(self):
        class PagedCodex(LocalCodex):
            def thread_page(self, search="", cursor=None, limit=25):
                chats = [
                    ChatSummary(id=str(index), title=f"Chat {index}", cwd=None, last_activity=index)
                    for index in reversed(range(125))
                    if search in f"Chat {index}"
                ]
                start = int(cursor or "0")
                end = start + limit
                return ChatPage(tuple(chats[start:end]), str(end) if end < len(chats) else None)

        with patch("carrie_on.codex.client.AppServer", PagedCodex):
            page = self.get("chats").json()
            self.assertEqual(len(page["items"]), 25)
            self.assertEqual(
                self.get(
                    "chats", params={"search": "other", "cursor": page["next_cursor"]}
                ).status_code,
                422,
            )
            ids = [chat["id"] for chat in page["items"]]
            while page["next_cursor"]:
                page = self.get("chats", params={"cursor": page["next_cursor"]}).json()
                ids.extend(chat["id"] for chat in page["items"])
            self.assertEqual(ids, [str(i) for i in range(124, -1, -1)])
            page = self.get("chats?search=Chat%201").json()
            self.assertTrue(all("Chat 1" in chat["title"] for chat in page["items"]))

    def test_chat_page_failure_is_retryable(self):
        with patch.object(LocalCodex, "thread_page", side_effect=CodexUnavailable("Try again")):
            self.assertEqual(self.get("chats").status_code, 503)
        self.assertEqual(self.get("chats").status_code, 200)

    def test_reset_before_enrollment_does_not_authorize_a_new_entry(self):
        reset = int(time.time()) + 3600
        with patch.object(
            LocalCodex, "usage", return_value=UsageSnapshot.parse(usage(100, reset - 3600))
        ):
            self.get("status")
        with patch.object(LocalCodex, "usage", return_value=UsageSnapshot.parse(usage(0, reset))):
            response = self.add()
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["reset_windows"], {"primary": reset})
            self.assertEqual(self.get("queue/check").json()["items"][0]["outcome"], "failed")
        with patch.object(
            LocalCodex, "usage", return_value=UsageSnapshot.parse(usage(0, reset + 3600))
        ):
            self.assertEqual(self.get("queue/check").json()["items"][0]["outcome"], "ready")

    def test_enrollment_requires_fresh_usage_and_ambiguous_buckets_require_selection(self):
        self.get("status")
        with patch.object(LocalCodex, "usage", side_effect=CodexUnavailable("Offline")):
            self.get("status")
            self.assertEqual(self.add().status_code, 503)
        raw = usage(25, int(time.time()) + 3600)
        raw["rateLimitsByLimitId"]["other"] = raw["rateLimitsByLimitId"]["codex"]
        with patch.object(LocalCodex, "usage", return_value=UsageSnapshot.parse(raw)):
            self.get("status")
            self.assertEqual(self.add().status_code, 422)
            response = self.add(limit_id="other")
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["limit_id"], "other")

    def test_late_usage_observation_cannot_replace_newer_account(self):
        entered, release = threading.Event(), threading.Event()

        def slow_usage(_):
            entered.set()
            self.assertTrue(release.wait(5))
            return UsageSnapshot.parse(usage(25, int(time.time()) + 3600, account="old"))

        with ThreadPoolExecutor(1) as pool, patch.object(LocalCodex, "usage", slow_usage):
            future = pool.submit(self.service.read_usage)
            try:
                self.assertTrue(entered.wait(3))
                with patch.object(
                    LocalCodex,
                    "usage",
                    return_value=UsageSnapshot.parse(usage(25, int(time.time()) + 3600)),
                ):
                    self.service.read_usage()
            finally:
                release.set()
            future.result()
        self.assertEqual(self.add().json()["account_id"], "account-a")

    def test_slow_enrollment_allows_other_queue_actions(self):
        existing = self.add().json()
        entered, release = threading.Event(), threading.Event()
        original = LocalCodex.snapshot

        def slow_snapshot(client, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(client, *args, **kwargs)

        with ThreadPoolExecutor(1) as pool, patch.object(LocalCodex, "snapshot", slow_snapshot):
            future = pool.submit(self.add, chat_id=str(uuid4()))
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.cancel(existing["id"]).status_code, 200)
            finally:
                release.set()
            self.assertEqual(future.result().status_code, 201)

    def test_concurrent_enrollment_rechecks_membership_and_identity_at_commit(self):
        barrier = threading.Barrier(2)
        original = LocalCodex.snapshot

        def simultaneous(client, *args, **kwargs):
            barrier.wait(timeout=5)
            return original(client, *args, **kwargs)

        for same_id in (True, False):
            item_id = str(uuid4())
            with ThreadPoolExecutor(2) as pool, patch.object(LocalCodex, "snapshot", simultaneous):
                futures = [
                    pool.submit(
                        self.add,
                        id=item_id if same_id else str(uuid4()),
                        instruction=f"Concurrent instruction {index}",
                    )
                    for index in range(2)
                ]
                responses = [future.result() for future in futures]
            self.assertEqual(
                sorted(response.status_code for response in responses),
                [200, 201] if same_id else [201, 409],
            )
            if same_id:
                self.assertEqual(
                    responses[0].json()["instruction"], responses[1].json()["instruction"]
                )
                self.assertIn(
                    responses[0].json()["instruction"],
                    {"Concurrent instruction 0", "Concurrent instruction 1"},
                )
            self.cancel(self.get("queue").json()["items"][0]["id"])

    def test_worker_failure_requests_restart_and_is_visible(self):
        restart = threading.Event()
        worker = DispatchWorker(self.store, self.connections, on_failure=restart.set)
        with patch(
            "carrie_on.dispatch.DispatchWorker.send", side_effect=AssertionError("Worker crashed")
        ):
            worker.thread.start()
            worker.thread.join(3)
        self.assertTrue(restart.is_set())
        self.assertEqual(worker.error, "Worker crashed")

    def test_doctor_and_readiness_inspection_do_not_create_deliveries(self):
        item = self.add().json()
        before = self.store.get_entry(UUID(item["id"]))
        report = self.get("diagnostics", params={"chat_id": CHAT_ID})
        self.assertEqual(report.json()["model_turns_started"], 0)
        readiness = self.get("queue/check")
        self.assertEqual(readiness.status_code, 200)
        self.assertEqual(readiness.json()["items"][0]["outcome"], "failed")
        self.assertIn("quota window", readiness.json()["items"][0]["reason"])
        self.assertEqual(self.store.get_entry(UUID(item["id"])), before)
        self.assertEqual(self.store.get_queue_summary().dispatching, 0)

    def test_declared_revision_and_headers_match_runtime_contract(self):
        schema = self.app.openapi()
        for path, operations in schema["paths"].items():
            for operation in operations.values():
                headers = {
                    parameter["name"].lower(): parameter
                    for parameter in operation.get("parameters", [])
                    if parameter["in"] == "header"
                }
                self.assertTrue(headers["x-carrie-on"]["required"], path)
        item_path = schema["paths"]["/api/v1/queue/{item_id}"]
        headers = {parameter["name"]: parameter for parameter in item_path["patch"]["parameters"]}
        self.assertNotIn("if-match", headers)
        edit_schema = item_path["patch"]["requestBody"]["content"]["application/json"]["schema"]
        edit_fields = schema["components"]["schemas"][edit_schema["$ref"].rsplit("/", 1)[1]]
        self.assertEqual(set(edit_fields["required"]), {"instruction", "revision"})
        self.assertEqual(edit_fields["properties"]["revision"]["type"], "integer")
        for method in ("get", "patch"):
            self.assertNotIn("ETag", item_path[method]["responses"]["200"].get("headers", {}))
        creation = schema["paths"]["/api/v1/queue"]["post"]["responses"]
        for status in ("200", "201"):
            self.assertEqual(set(creation[status]["headers"]), {"Location"})
        unhealthy = schema["paths"]["/api/v1/health"]["get"]["responses"]["503"]["content"]
        self.assertEqual(set(unhealthy), {"application/json", "application/problem+json"})
        self.assertEqual(
            unhealthy["application/json"]["schema"]["$ref"], "#/components/schemas/HealthResponse"
        )

    def test_cursor_numbers_rejected_before_sqlite_binding(self):
        self.add()
        cases = [
            ("queue", ["queue", ["queued", "failed", "stale"], None], [value, str(uuid4())])
            for value in (2**100, -(2**100), 10**1000, float("inf"), float("nan"))
        ]
        for path, scope, position in cases:
            with self.subTest(path=path, position=position):
                cursor = base64.urlsafe_b64encode(json.dumps([scope, position]).encode()).decode()
                response = self.get(path, params={"cursor": cursor})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers["content-type"], "application/problem+json")
                self.assertEqual(response.json()["code"], "invalid_cursor")

    def test_expected_upstream_failure_and_programming_error_are_distinct(self):
        with patch.object(LocalCodex, "usage", side_effect=CodexProtocolError("Incomplete quota")):
            response = self.get("usage")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "unavailable")
        with patch.object(LocalCodex, "usage", side_effect=TypeError("Programming error")):
            with self.assertRaisesRegex(TypeError, "Programming error"):
                self.get("usage")

    def test_instruction_nul_is_preserved_by_create_and_edit(self):
        item = self.add(instruction="\x00Original").json()
        response = self.edit(item["id"], "\x00Edited", item["revision"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["instruction"], "\x00Edited")
        self.assertEqual(self.get(f"queue/{item['id']}").json()["instruction"], "\x00Edited")

    def test_invalid_revisions_do_not_change_instructions(self):
        item = self.add().json()
        for revision in (None, True, False, 0, -1, 1.0, "1", "9" * 5000, {}, []):
            with self.subTest(revision=revision):
                response = self.edit(item["id"], "Changed", revision)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")
        response = self.edit(item["id"], "Changed", 10**1000)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "stale_revision")
        saved = self.get(f"queue/{item['id']}").json()
        self.assertEqual(saved["instruction"], "Continue tests")
        self.assertEqual(saved["revision"], 1)

    def test_method_errors_retain_allow(self):
        response = self.client.put("/api/v1/queue", json={})
        self.assertEqual(response.status_code, 405)
        self.assertIn("Allow", response.headers)
        self.assertEqual(response.json()["status"], 405)
