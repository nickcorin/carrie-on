"""Test dispatch decisions with SQLite and synthetic Codex responses."""

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
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
from carrie_on.dispatch import DispatchWorker
from carrie_on.queue import QueueEntry
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance
from tests.fixtures import CHAT_ID, CodexFixture, instant, snapshot, usage


class DispatchBehavior(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "queue.sqlite3"
        SqliteStorageMaintenance(self.path).initialize()
        self.now = 110
        self.store = SqliteQueueStorage(
            self.path, clock=lambda: instant(self.now), lease_duration=timedelta(seconds=1)
        )
        self.codex = CodexFixture()
        self.item = QueueEntry.enroll(
            uuid4(),
            "Finish tests",
            Chat.parse(snapshot()),
            UsageSnapshot.parse(usage()).select(),
            instant(50),
        )
        self.store.create_entry(self.item)
        self.connections = CodexConnections("fixture")
        self.worker = DispatchWorker(self.store, self.connections, clock=lambda: instant(self.now))

    def tick(self, now=None):
        self.now = self.now + 2 if now is None else now
        with (
            patch.object(CodexConnections, "app_server", return_value=self.codex),
            patch.object(CodexConnections, "desktop", return_value=self.codex),
        ):
            return self.worker.send()

    def item_state(self):
        return self.store.get_entry(self.item.id)

    def replace_item(self, **values):
        self.now += 2
        self.store.update_entry_state(self.item.id, "cancelled")
        self.item = replace(self.item, id=uuid4(), **values)
        self.store.create_entry(self.item)

    def test_reset_sends_exact_instruction_once_and_retains_history(self):
        self.assertTrue(self.tick())
        self.assertFalse(self.tick())
        self.assertEqual(
            self.codex.delivered,
            [{"thread_id": CHAT_ID, "prompt": "Finish tests", "message_id": str(self.item.id)}],
        )
        self.assertEqual(self.item_state().state, "sent")
        self.assertEqual(self.store.list_entries()[0], [])
        self.assertEqual(self.store.list_entries(states=("sent",))[0][0].id, self.item.id)

    def test_reads_and_unrelated_edits_remain_available_during_network_io(self):
        other = replace(self.item, id=uuid4(), chat_id=uuid4())
        self.store.create_entry(other)

        def send(*args):
            self.assertTrue(self.store.get_entry(self.item.id).dispatching)
            edited = self.store.update_instructions(other.id, "Other instruction", revision=1)
            self.assertEqual(edited.instructions, "Other instruction")
            return "receipt"

        with patch.object(self.codex, "continue_chat", side_effect=send):
            self.tick()

    def test_exhausted_quota_fails_and_later_reset_retries(self):
        self.codex.limits = usage(100, 200)
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertTrue(self.item_state().dispatching)
        self.codex.limits = usage(0, 200)
        self.tick()
        self.assertEqual(len(self.codex.delivered), 1)

    def test_changed_window_does_not_wait_for_saved_deadline(self):
        self.codex.limits = usage(0, 200)
        self.tick(now=70)
        self.assertEqual(self.item_state().state, "sent")

    def test_changed_window_identity_need_not_increase(self):
        self.codex.limits = usage(0, 90)
        self.tick(now=70)
        self.assertEqual(self.item_state().state, "sent")

    def test_percentage_drop_and_elapsed_deadline_do_not_change_window_identity(self):
        self.codex.limits = usage(30, 100)
        self.tick(now=110)
        self.assertEqual(self.item_state().state, "failed")
        self.assertEqual(self.codex.delivered, [])
        self.codex.limits = usage(0, 100)
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertEqual(self.codex.delivered, [])
        self.codex.limits = usage(0, 200)
        self.tick()
        self.assertEqual(self.item_state().state, "sent")

    def test_first_reset_waits_for_current_capacity_without_requiring_all_deadlines_to_advance(
        self,
    ):
        self.replace_item(reset_windows={"primary": 100, "secondary": 500})
        self.codex.limits = usage(0, 200, {"usedPercent": 100, "resetsAt": 500})
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertEqual(self.codex.delivered, [])
        self.codex.limits = usage(0, 200, {"usedPercent": 0, "resetsAt": 500})
        self.tick()
        self.assertEqual(self.item_state().state, "sent")

    def test_secondary_window_change_after_restart_needs_no_observed_exhaustion(self):
        self.store.update_entry_state(self.item.id, "cancelled")
        self.codex.chat = snapshot("completed", error=None)
        self.item = QueueEntry.enroll(
            uuid4(),
            "Finish tests",
            Chat.parse(self.codex.chat),
            UsageSnapshot.parse(usage(20, 100, {"usedPercent": 40, "resetsAt": 500})).select(),
            instant(50),
        )
        self.store.create_entry(self.item)
        self.store = SqliteQueueStorage(self.path, clock=lambda: instant(self.now))
        self.worker = DispatchWorker(self.store, self.connections, clock=lambda: instant(self.now))
        self.codex.limits = usage(20, 100, {"usedPercent": 60, "resetsAt": 600})
        self.tick(now=70)
        self.assertEqual(self.item_state().state, "sent")
        self.assertEqual(
            self.codex.delivered,
            [{"thread_id": CHAT_ID, "prompt": "Finish tests", "message_id": str(self.item.id)}],
        )

    def test_unrecorded_window_does_not_establish_a_changed_window(self):
        self.codex.limits = usage(0, 100, {"usedPercent": 0, "resetsAt": 500})
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertEqual(self.codex.delivered, [])
        self.codex.limits = usage(0, 200, {"usedPercent": 0, "resetsAt": 500})
        self.tick()
        self.assertEqual(self.item_state().state, "sent")

    def test_a_reset_does_not_override_reported_usage_or_spending_blocks(self):
        for flag in ("ordinary", "spending", "rate_limit"):
            self.codex.limits = usage(0, 200)
            bucket = self.codex.limits["rateLimitsByLimitId"]["codex"]
            if flag == "ordinary":
                self.codex.limits["ordinaryUsageAllowed"] = False
            elif flag == "spending":
                bucket["spendControlReached"] = True
            else:
                bucket["rateLimitReachedType"] = "weekly"
            self.tick()
            self.assertEqual(self.item_state().state, "failed")
        self.assertEqual(self.codex.delivered, [])
        self.codex.limits = usage(0, 200)
        self.tick()
        self.assertEqual(self.item_state().state, "sent")

    def test_unrecorded_weekly_exhaustion_still_blocks_dispatch(self):
        self.codex.limits = usage(0, 200, {"usedPercent": 100, "resetsAt": 500})
        self.tick()
        self.assertEqual(self.item_state().state, "failed")

    def test_account_change_is_a_visible_failure(self):
        self.codex.limits = usage(0, 200, account="different-account")
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertIn("account", self.item_state().error)
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
        self.assertEqual(self.item_state().error, "")

    def test_busy_chat_records_failure_then_retries(self):
        self.codex.chat = snapshot("inProgress", runtime="active")
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertTrue(self.item_state().error)
        self.codex.chat = snapshot()
        self.tick()
        self.assertEqual(self.item_state().state, "sent")

    def test_approval_input_and_goal_blocks_record_retryable_errors(self):
        for change in (
            {"requests": [{}]},
            {"externalRequests": [{}]},
            {"threadGoal": {"status": "paused"}},
            {"threadGoal": {"status": "blocked"}},
            {"threadGoal": {"status": "budgetLimited"}},
        ):
            self.codex.chat = {**snapshot(), **change}
            self.tick()
            self.assertEqual(self.item_state().state, "failed")
            self.assertTrue(self.item_state().error)
        self.assertFalse(self.codex.delivered)

    def test_changed_task_is_stale_but_model_or_catalog_failures_retry(self):
        self.codex.chat = {**snapshot(), "latestModel": "new-model"}
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.codex.chat = snapshot(message_id="different-task")
        self.tick()
        self.assertEqual(self.item_state().state, "stale")
        self.replace_item()
        self.codex.chat = snapshot()
        with patch.object(self.codex, "threads", return_value=[]):
            self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertFalse(self.codex.delivered)

    def test_busy_enrollment_completed_normally_is_not_resumed(self):
        self.replace_item(enrolled_while_busy=True)
        self.codex.chat = snapshot("completed")
        self.tick()
        self.assertEqual(self.item_state().state, "stale")
        self.assertFalse(self.codex.delivered)

    def test_busy_enrollment_can_resume_after_window_change_without_quota_failure(self):
        self.replace_item(enrolled_while_busy=True)
        self.codex.chat = snapshot("interrupted", error="other")
        self.codex.limits = usage(50, 100)
        self.tick()
        self.assertEqual(self.item_state().state, "failed")
        self.assertFalse(self.codex.delivered)
        self.codex.limits = usage(50, 200)
        self.tick()
        self.assertEqual(self.item_state().state, "sent")

    def test_enrollment_captures_anchors_without_deciding_readiness(self):
        for changes in (
            {"requests": [{}]},
            {"threadGoal": {"status": "paused"}},
            {"resumeState": "loading"},
        ):
            item = QueueEntry.enroll(
                uuid4(),
                "Continue",
                Chat.parse({**snapshot(), **changes}),
                UsageSnapshot.parse(usage()).select(),
                instant(50),
            )
            self.assertEqual(item.state, "queued")

    def test_lost_receipt_reconciles_accepted_message_without_resending(self):
        def accepted_but_lost(*_):
            self.codex.chat = snapshot("inProgress", str(self.item.id), "active")
            raise CodexUnavailable("Receipt lost")

        with patch.object(self.codex, "continue_chat", side_effect=accepted_but_lost) as send:
            self.tick()
            self.assertEqual(self.item_state().state, "failed")
            self.store = SqliteQueueStorage(self.path, clock=lambda: instant(self.now))
            self.worker.store = self.store
            self.tick(now=114)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.item_state().state, "sent")

    def test_earlier_accepted_message_is_reconciled_before_marking_stale(self):
        self.codex.chat = snapshot(message_id="later-message")
        with patch.object(self.codex, "has_message", return_value=True) as find:
            self.tick()
        find.assert_called_once_with(CHAT_ID, str(self.item.id))
        self.assertEqual(self.item_state().state, "sent")
        self.assertFalse(self.codex.delivered)

    def test_unchanged_anchor_does_not_scan_earlier_conversation_history(self):
        with patch.object(
            self.codex, "has_message", side_effect=AssertionError("Unnecessary history scan")
        ):
            self.tick()
        self.assertEqual(self.item_state().state, "sent")

    def test_uncertain_unaccepted_send_can_retry_with_stable_message_id(self):
        self.codex.lose_reply = True
        self.tick()
        self.codex.lose_reply = False
        self.tick(now=114)
        self.assertEqual(
            [send["message_id"] for send in self.codex.delivered],
            [str(self.item.id), str(self.item.id)],
        )

    def test_provisional_message_is_not_an_acceptance_receipt(self):
        raw = snapshot(message_id=str(self.item.id))
        raw["turnHistory"]["history"]["entitiesByKey"]["latest"].pop("turnId")
        self.assertNotIn(str(self.item.id), Chat.parse(raw).seen_message_ids)
        self.codex.chat = raw
        self.tick()
        self.assertEqual(self.item_state().state, "stale")
        self.assertFalse(self.codex.delivered)

    def test_connection_failure_is_visible_and_does_not_lose_work(self):
        def unavailable(*args, **kwargs):
            raise CodexUnavailable("No desktop")

        with patch.object(CodexConnections, "app_server", side_effect=unavailable):
            self.worker.send()
        self.assertEqual(self.item_state().error, "No desktop")
        self.assertEqual(self.item_state().state, "failed")

    def test_ambiguous_quota_requires_selection_and_read_client_rejects_execution(self):
        raw = usage()
        raw["rateLimitsByLimitId"]["other"] = raw["rateLimitsByLimitId"]["codex"]
        with self.assertRaisesRegex(UsageSelectionError, "Choose --limit-id"):
            UsageSnapshot.parse(raw).select()
        self.assertEqual(UsageSnapshot.parse(raw).select("other").limit_id, "other")
        self.assertFalse(hasattr(AppServer, "request"))

    def test_programming_and_persistence_failures_escape_without_reclassification(self):
        with patch.object(self.codex, "usage", side_effect=TypeError("Bug in adapter")):
            with self.assertRaisesRegex(TypeError, "Bug in adapter"):
                self.tick()
        self.assertEqual(self.item_state().state, "queued")
        self.assertTrue(self.item_state().dispatching)
        with patch.object(
            self.store, "update_entry_state", side_effect=RuntimeError("Storage bug")
        ):
            with self.assertRaisesRegex(RuntimeError, "Storage bug"):
                self.tick(now=114)
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
