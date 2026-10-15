"""Quota and conversation safeguards exercised against a real SQLite queue."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from carrie_on.codex.client import AppServer, CodexConnections
from carrie_on.codex.models import (
    Chat,
    CodexProtocolError,
    CodexUnavailable,
    UsageSelectionError,
    UsageSnapshot,
)
from carrie_on.dispatch import dispatch_next
from carrie_on.queue import EnqueueCommand, QueueItem
from carrie_on.storage import QueueStore
from tests.fixtures import CodexFixture, snapshot, usage


class DispatchBehavior(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = QueueStore(Path(directory.name) / "queue.sqlite3")
        self.store.initialize()
        self.codex = CodexFixture()
        self.command = EnqueueCommand(
            id=str(uuid4()), chat_id="thread-a", instruction="Finish tests"
        )
        self.item = QueueItem.enroll(
            self.command, Chat.parse(snapshot()), UsageSnapshot.parse(usage()).select(), 50
        )
        self.store.enqueue(self.item, self.command)
        self.connections = CodexConnections("fixture")

    def tick(self, now=110):
        with (
            patch.object(CodexConnections, "app_server", return_value=self.codex),
            patch.object(CodexConnections, "desktop", return_value=self.codex),
        ):
            return dispatch_next(self.store, self.connections, clock=lambda: now)

    def item_state(self):
        return self.store.get(self.item.id).item

    def replace_item(self, **values):
        with self.store.connection(write=True) as connection:
            connection.execute("DELETE FROM queue_items")
        self.item = replace(self.item, **values)
        self.store.enqueue(self.item, replace(self.command, id=self.item.id))

    def test_reset_sends_exact_instruction_once_and_retains_history(self):
        self.assertTrue(self.tick())
        self.assertFalse(self.tick())
        self.assertEqual(
            self.codex.delivered,
            [{"thread_id": "thread-a", "prompt": "Finish tests", "message_id": self.item.id}],
        )
        self.assertEqual(self.item_state().state, "sent")
        self.assertEqual(self.store.page()[0], [])
        self.assertEqual(self.store.deliveries(self.item.id)[0][0].outcome, "sent")
        self.assertEqual(self.store.deliveries(self.item.id)[0][0].receipt, "continued")

    def test_reads_and_unrelated_edits_remain_available_during_network_io(self):
        other = replace(self.item, id=str(uuid4()), chat_id="thread-b")
        self.store.enqueue(other, replace(self.command, id=other.id, chat_id=other.chat_id))

        def send(*args):
            delivery = self.store.get(self.item.id).active_delivery
            self.assertEqual(delivery.sending_at, 110)
            self.assertEqual(delivery.lease_expires_at, 230)
            edited = self.store.edit(other.id, "Other instruction", 1, now=110)
            self.assertEqual(edited.item.instruction, "Other instruction")
            return "receipt"

        with patch.object(self.codex, "continue_chat", side_effect=send):
            self.tick()

    def test_exhausted_quota_defers_and_later_reset_sends(self):
        self.codex.limits = usage(100, 200)
        self.tick()
        self.assertEqual((self.item_state().state, self.item_state().deferrals), ("queued", 1))
        self.assertIsNone(self.store.get(self.item.id).active_delivery)
        self.codex.limits = usage(0, 200)
        self.tick()
        self.assertEqual(len(self.codex.delivered), 1)

    def test_early_manual_reset_does_not_wait_for_old_deadline(self):
        self.tick(now=70)
        self.assertEqual(self.item_state().state, "sent")

    def test_headroom_without_advanced_or_future_timestamp_waits(self):
        for reset in (100, 105):
            self.codex.limits = usage(0, reset)
            self.tick(now=110)
            self.assertEqual(self.item_state().state, "queued")
        self.assertFalse(self.codex.delivered)

    def test_all_exhausted_windows_must_advance_and_all_windows_need_headroom(self):
        self.replace_item(reset_windows={"primary": 100, "secondary": 500})
        for secondary in (
            {"usedPercent": 100, "resetsAt": 600},
            {"usedPercent": 0, "resetsAt": 500},
            None,
        ):
            self.codex.limits = usage(0, 200, secondary)
            self.tick()
            self.assertEqual(self.item_state().state, "queued")
        self.codex.limits = usage(0, 200, {"usedPercent": 0, "resetsAt": 600})
        self.tick()
        self.assertEqual(self.item_state().state, "sent")

    def test_unrecorded_weekly_exhaustion_still_blocks_dispatch(self):
        self.codex.limits = usage(0, 200, {"usedPercent": 100, "resetsAt": 500})
        self.tick()
        self.assertEqual(self.item_state().state, "queued")

    def test_account_change_is_a_visible_failure(self):
        self.codex.limits = usage(0, 200, account="different-account")
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertIn("account", self.item_state().reason)
        self.assertFalse(self.codex.delivered)

    def test_unknown_quota_never_authorizes_dispatch(self):
        self.codex.limits = {}
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertFalse(self.codex.delivered)

    def test_network_failure_recovers_and_clears_card_error(self):
        self.codex.usage_error = True
        self.tick()
        self.assertEqual(self.item_state().error, "Usage unavailable")
        self.codex.usage_error = False
        self.tick()
        self.assertEqual(self.item_state().state, "sent")
        self.assertIsNone(self.item_state().error)

    def test_busy_chat_defers_without_error_then_sends(self):
        self.codex.chat = snapshot("inProgress", runtime="active")
        self.tick()
        self.assertEqual(self.item_state().state, "queued")
        self.assertIsNone(self.item_state().error)
        self.codex.chat = snapshot()
        self.tick()
        self.assertEqual(self.item_state().state, "sent")

    def test_approval_input_and_goal_blocks_wait_without_errors(self):
        for change in (
            {"requests": [{}]},
            {"externalRequests": [{}]},
            {"threadGoal": {"status": "paused"}},
            {"threadGoal": {"status": "blocked"}},
            {"threadGoal": {"status": "budgetLimited"}},
        ):
            self.codex.chat = {**snapshot(), **change}
            self.tick()
            self.assertEqual(self.item_state().state, "queued")
            self.assertIsNone(self.item_state().error)
        self.assertFalse(self.codex.delivered)

    def test_changed_task_model_or_catalog_never_sends(self):
        for change in ({"latestModel": "new-model"}, snapshot(message_id="different-task")):
            with self.subTest(change=change):
                self.codex.chat = {**snapshot(), **change}
                self.tick(now=70)
                self.assertEqual(self.item_state().state, "changed")
                self.assertFalse(self.codex.delivered)
                self.store.cancel(self.item.id, now=71)
                self.item = replace(self.item, id=str(uuid4()))
                self.store.enqueue(self.item, replace(self.command, id=self.item.id))
        self.codex.chat = snapshot()
        with patch.object(self.codex, "threads", return_value=[]):
            self.tick()
        self.assertEqual(self.item_state().state, "changed")

    def test_busy_enrollment_completed_or_interrupted_is_not_resumed(self):
        self.replace_item(enrolled_while_busy=True)
        self.codex.chat = snapshot("completed")
        self.tick()
        self.assertEqual(self.item_state().state, "changed")
        self.assertIn("finished", self.item_state().reason)
        self.assertFalse(self.codex.delivered)

    def test_manual_interruption_is_not_a_quota_failure(self):
        self.replace_item(enrolled_while_busy=True)
        self.codex.chat = snapshot("interrupted", error="other")
        self.tick()
        self.assertEqual(self.item_state().state, "changed")
        self.assertFalse(self.codex.delivered)

    def test_enrollment_captures_anchors_without_deciding_readiness(self):
        for changes in (
            {"requests": [{}]},
            {"threadGoal": {"status": "paused"}},
            {"resumeState": "loading"},
        ):
            item = QueueItem.enroll(
                EnqueueCommand(id=str(uuid4()), chat_id="thread-a", instruction="Continue"),
                Chat.parse({**snapshot(), **changes}),
                UsageSnapshot.parse(usage()).select(),
                50,
            )
            self.assertEqual(item.state, "queued")

    def test_lost_receipt_reconciles_accepted_message_without_resending(self):
        def accepted_but_lost(*_):
            self.codex.chat = snapshot("inProgress", self.item.id, "active")
            raise CodexUnavailable("Receipt lost")

        with patch.object(self.codex, "continue_chat", side_effect=accepted_but_lost) as send:
            self.tick()
            self.assertEqual(self.item_state().state, "queued")
            self.store = QueueStore(self.store.path)
            self.tick(now=111)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.item_state().state, "sent")

    def test_uncertain_unaccepted_send_can_retry_with_stable_message_id(self):
        self.codex.lose_reply = True
        self.tick()
        self.codex.lose_reply = False
        self.tick(now=111)
        self.assertEqual(
            [send["message_id"] for send in self.codex.delivered], [self.item.id, self.item.id]
        )
        attempts, _ = self.store.deliveries(self.item.id)
        self.assertEqual(len({attempt.id for attempt in attempts}), 2)

    def test_provisional_message_is_not_an_acceptance_receipt(self):
        raw = snapshot(message_id=self.item.id)
        raw["turnHistory"]["history"]["entitiesByKey"]["latest"].pop("turnId")
        self.assertNotIn(self.item.id, Chat.parse(raw).seen_message_ids)
        self.codex.chat = raw
        self.tick()
        self.assertEqual(self.item_state().state, "changed")
        self.assertFalse(self.codex.delivered)

    def test_connection_failure_is_visible_and_does_not_lose_work(self):
        def unavailable(*args, **kwargs):
            raise CodexUnavailable("No desktop")

        with patch.object(CodexConnections, "app_server", side_effect=unavailable):
            dispatch_next(self.store, self.connections, clock=lambda: 110)
        self.assertEqual(self.item_state().error, "No desktop")
        self.assertEqual(self.item_state().state, "queued")

    def test_ambiguous_quota_requires_selection_and_read_client_rejects_execution(self):
        raw = usage()
        raw["rateLimitsByLimitId"]["other"] = raw["rateLimitsByLimitId"]["codex"]
        with self.assertRaisesRegex(UsageSelectionError, "Choose --limit-id"):
            UsageSnapshot.parse(raw).select()
        self.assertEqual(UsageSnapshot.parse(raw).select("other").limit_id, "other")
        with self.assertRaisesRegex(ValueError, "read-only"):
            AppServer().request("turn/start", {})

    def test_programming_and_persistence_failures_escape_without_reclassification(self):
        with patch.object(self.codex, "usage", side_effect=TypeError("Bug in adapter")):
            with self.assertRaisesRegex(TypeError, "Bug in adapter"):
                self.tick()
        self.assertEqual(self.item_state().state, "queued")
        self.assertIsNotNone(self.store.get(self.item.id).active_delivery)
        with patch.object(self.store, "finish", side_effect=RuntimeError("Storage bug")):
            with self.assertRaisesRegex(RuntimeError, "Storage bug"):
                self.tick(now=231)
        self.assertEqual(self.item_state().state, "queued")

    def test_malformed_codex_fields_are_protocol_errors_and_never_send(self):
        invalid_chats = [
            {"title": {}},
            {"threadRuntimeStatus": {"type": []}},
            {"threadGoal": {"status": {}}},
        ]
        for changes in invalid_chats:
            with self.subTest(changes=changes), self.assertRaises(CodexProtocolError):
                Chat.parse({**snapshot(), **changes})
        raw = snapshot()
        raw["turnHistory"]["history"]["entitiesByKey"]["latest"]["status"] = {}
        with self.assertRaises(CodexProtocolError):
            Chat.parse(raw)
        for changes in (
            {"spendControlReached": "true"},
            {"primary": {"usedPercent": 10**1000, "resetsAt": 200}},
        ):
            raw_usage = usage(0, 200)
            raw_usage["rateLimitsByLimitId"]["codex"].update(changes)
            with self.subTest(changes=list(changes)), self.assertRaises(CodexProtocolError):
                UsageSnapshot.parse(raw_usage)
        self.codex.limits = raw_usage
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertEqual(self.codex.delivered, [])
