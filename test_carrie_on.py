"""Observable queue behavior using synthetic quota and desktop responses."""

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_bridge import AppServer, BridgeError
from resume_queue import Chat, Entry, Usage, check_queue, locked_queue
import carrie_on


def usage(used=100, reset=100, secondary=None, account="account-a"):
    return {
        "accountId": account, "ordinaryUsageAllowed": used < 100,
        "rateLimitsByLimitId": {"codex": {
            "primary": {"usedPercent": used, "resetsAt": reset},
            "secondary": secondary,
        }},
    }


def snapshot(status="failed", message_id="original", runtime="idle", error="usageLimitExceeded"):
    return {
        "id": "thread-a", "title": "Finish feature", "latestTurnStartMessageId": message_id,
        "latestModel": "model-a", "resumeState": "resumed", "requests": [],
        "threadRuntimeStatus": {"type": runtime},
        "turnHistory": {"kind": "canonical", "history": {"entitiesByKey": {
            "latest": {"turnId": "turn-a", "status": status, "error": {"codexErrorInfo": error},
                       "params": {"clientUserMessageId": message_id}},
        }}},
    }


class CodexFixture:
    def __init__(self):
        self.limits = usage(0, 200)
        self.chat = snapshot()
        self.delivered = []
        self.lose_reply = False
        self.usage_error = False

    def usage(self):
        if self.usage_error:
            raise BridgeError("Usage unavailable")
        return self.limits

    def snapshot(self, _thread):
        return self.chat

    def threads(self, _search):
        return [{"id": "thread-a"}]

    def continue_chat(self, thread_id, prompt, message_id):
        self.delivered.append({"thread_id": thread_id, "prompt": prompt, "message_id": message_id})
        if self.lose_reply:
            raise BridgeError("Reply lost")
        self.chat = snapshot("inProgress", message_id, "active")
        return {"result": {"turn": {"id": "continued"}}}


class QueueBehavior(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "queue.json"
        self.codex = CodexFixture()
        with locked_queue(self.path) as queue:
            entry = Entry.enroll(Chat.parse(snapshot()), Usage.parse(usage()), "Finish tests", 50)
            self.entry_id = entry.id
            queue.entries.append(entry)
            queue.save()

    def tick(self, now=110):
        with locked_queue(self.path) as queue:
            return check_queue(queue, self.codex, self.codex, now)

    def state(self):
        return json.loads(self.path.read_text())

    def test_reset_sends_exact_prompt_once_and_removes_enrollment(self):
        self.assertEqual(self.tick(), ["Continuation accepted: Finish feature"])
        self.tick()
        self.assertEqual(self.codex.delivered, [{"thread_id": "thread-a", "prompt": "Finish tests",
                                               "message_id": self.entry_id}])
        self.assertEqual(self.state()["history"][0]["outcome"], "resumed")
        self.assertEqual(self.state()["entries"], [])

    def test_status_shows_processing_without_waiting_for_worker_lock(self):
        args = carrie_on.parser().parse_args(["--state", str(self.path), "list"])

        def read_usage():
            entry = carrie_on.run(args)["entries"][0]
            self.assertEqual((entry["state"], entry["processing_until"]), ("waiting", None))
            return usage(0, 200)

        def send(thread_id, prompt, message_id):
            entry = carrie_on.run(args)["entries"][0]
            self.assertEqual((entry["state"], entry["processing_until"]), ("sending", 230))
            self.assertEqual((thread_id, prompt, message_id), ("thread-a", "Finish tests", self.entry_id))

        with patch.object(self.codex, "usage", side_effect=read_usage), \
                patch.object(self.codex, "continue_chat", side_effect=send):
            self.assertEqual(self.tick(), ["Continuation accepted: Finish feature"])
        self.assertIsNone(self.state()["history"][0]["processing_until"])

    def test_processing_clears_after_wait_and_error(self):
        self.codex.limits = usage(100, 100)
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "waiting")
        self.assertIsNone(self.state()["entries"][0]["processing_until"])
        self.codex.usage_error = True
        self.tick()
        entry = self.state()["entries"][0]
        self.assertEqual(entry["reactivation_error"], "Usage unavailable")
        self.assertIsNone(entry["processing_until"])

    def test_no_model_turn_while_exhausted_then_resumes(self):
        self.codex.limits = usage(100, 200)
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "waiting")
        self.codex.limits = usage(0, 200)
        self.tick()
        self.assertEqual(len(self.codex.delivered), 1)

    def test_early_reset_sends_once_before_original_deadline(self):
        self.codex.limits = usage(100, 100)
        self.tick(now=60)
        self.assertEqual(self.state()["entries"][0]["state"], "waiting")
        self.codex.limits = usage(0, 200)
        self.tick(now=70)
        self.tick(now=80)
        self.assertEqual(self.codex.delivered, [{"thread_id": "thread-a", "prompt": "Finish tests",
                                               "message_id": self.entry_id}])
        self.assertEqual(self.state()["history"][0]["outcome"], "resumed")

    def test_headroom_without_advanced_reset_still_waits(self):
        self.codex.limits = usage(0, 100)
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "waiting")
        self.codex.limits = usage(0, 200)
        self.tick()
        self.assertEqual(len(self.codex.delivered), 1)

    def test_elapsed_replacement_reset_is_not_fresh_confirmation(self):
        self.codex.limits = usage(0, 105)
        self.tick(110)
        self.assertEqual(self.state()["entries"][0]["state"], "waiting")
        self.codex.limits = usage(0, 200)
        self.tick(110)
        self.assertEqual(self.state()["history"][0]["outcome"], "resumed")

    def test_early_reset_still_holds_changed_chat(self):
        self.codex.chat = snapshot(message_id="new-human-message")
        self.tick(70)
        self.assertEqual(self.state()["entries"][0]["state"], "needs_attention")
        self.assertEqual(self.state()["entries"][0]["note"],
                         "Chat changed after enrollment; remove and re-enroll if still wanted")
        self.assertEqual(self.codex.delivered, [])

    def test_weekly_exhaustion_blocks_short_window_reset(self):
        self.codex.limits = usage(0, 200, {"usedPercent": 100, "resetsAt": 1000})
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "waiting")
        self.codex.limits = usage(0, 200, {"usedPercent": 0, "resetsAt": 2000})
        self.tick()
        self.assertEqual(len(self.codex.delivered), 1)

    def test_all_originally_exhausted_windows_must_reset(self):
        with locked_queue(self.path) as queue:
            queue.entries[0].reset_windows["secondary"] = 1000
            queue.save()
        self.codex.limits = usage(0, 200, {"usedPercent": 0, "resetsAt": 1000})
        self.tick(90)
        self.assertEqual(self.state()["entries"][0]["state"], "waiting")
        self.codex.limits = usage(0, 200, {"usedPercent": 0, "resetsAt": 2000})
        self.tick(95)
        self.assertEqual(len(self.codex.delivered), 1)

    def test_cli_tick_observes_reset_before_deadline(self):
        args = carrie_on.parser().parse_args(["--state", str(self.path), "tick"])
        with patch("carrie_on.time.time", return_value=70), \
                patch("carrie_on.AppServer") as server, patch("carrie_on.Desktop") as desktop:
            server.return_value.__enter__.return_value = self.codex
            desktop.return_value.__enter__.return_value = self.codex
            self.assertEqual(carrie_on.run(args),
                             {"status": "checked", "outcomes": ["Continuation accepted: Finish feature"]})
        self.assertEqual(self.state()["history"][0]["outcome"], "resumed")

    def test_unknown_usage_is_held_and_never_assumed_available(self):
        self.codex.limits["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = None
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "needs_attention")
        self.assertEqual(self.state()["entries"][0]["note"], "Incomplete primary quota information")

    def test_network_failure_recovers_without_losing_entry(self):
        self.codex.usage_error = True
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "waiting")
        self.assertEqual(self.state()["entries"][0]["reactivation_error"], "Usage unavailable")
        self.codex.usage_error = False
        self.codex.limits = usage(100, 200)
        self.tick()
        self.assertIsNone(self.state()["entries"][0]["reactivation_error"])
        self.codex.limits = usage(0, 200)
        self.tick()
        self.assertEqual(len(self.codex.delivered), 1)

    def test_account_change_requires_attention(self):
        self.codex.limits = usage(0, 200, account="different-account")
        self.tick()
        self.assertEqual(self.state()["entries"][0]["note"], "Codex account or quota bucket changed")

    def test_busy_chat_is_skipped_then_continued_when_idle(self):
        self.codex.chat = snapshot("inProgress", runtime="active")
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "waiting")
        self.codex.chat = snapshot()
        self.tick()
        self.assertEqual(len(self.codex.delivered), 1)

    def test_changed_chat_is_not_resumed(self):
        self.codex.chat = snapshot(message_id="new-human-message")
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "needs_attention")
        self.assertEqual(self.state()["entries"][0]["note"],
                         "Chat changed after enrollment; remove and re-enroll if still wanted")
        self.assertEqual(self.state()["entries"][0]["reactivation_error"],
                         "Chat changed after enrollment; remove and re-enroll if still wanted")

    def test_completed_work_is_removed_if_enrolled_while_running(self):
        with locked_queue(self.path) as queue:
            queue.entries[0].enrolled_while_busy = True
            queue.save()
        self.codex.chat = snapshot("completed", error=None)
        self.tick()
        self.assertEqual(self.state()["history"][0]["outcome"], "finished_before_reset")

    def test_manual_interruption_is_not_mistaken_for_quota_failure(self):
        with locked_queue(self.path) as queue:
            queue.entries[0].enrolled_while_busy = True
            queue.save()
        self.codex.chat = snapshot("interrupted", error=None)
        self.tick()
        self.assertEqual(self.state()["entries"][0]["note"],
                         "Chat stopped for a reason other than quota exhaustion")

    def test_paused_goal_is_not_resumed(self):
        self.codex.chat["threadGoal"] = {"status": "paused"}
        self.tick()
        self.assertEqual(self.state()["entries"][0]["note"], "Waiting: chat is active or requires attention")
        del self.codex.chat["threadGoal"]
        self.tick()
        self.assertEqual(len(self.codex.delivered), 1)

    def test_readiness_is_deferred_from_enrollment_to_dispatch(self):
        blocked = snapshot()
        blocked["threadGoal"] = {"status": "paused"}
        blocked["requests"] = [{"kind": "approval"}]
        with locked_queue(self.path) as queue:
            queue.entries = [Entry.enroll(Chat.parse(blocked), Usage.parse(usage()), "Wait for approval", 50)]
            queue.save()
        self.codex.chat = blocked
        self.tick()
        entry = self.state()["entries"][0]
        self.assertEqual((entry["state"], entry["note"]),
                         ("waiting", "Waiting: chat is active or requires attention"))
        self.assertEqual(self.codex.delivered, [])
        self.codex.chat = snapshot()
        self.tick()
        self.assertEqual(self.codex.delivered[0]["prompt"], "Wait for approval")
        self.assertEqual(self.state()["history"][0]["outcome"], "resumed")

    def test_lost_reply_does_not_duplicate_on_restart(self):
        self.codex.lose_reply = True
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "sending")
        self.assertEqual(self.state()["entries"][0]["reactivation_error"], "Reply lost")
        self.codex.chat = snapshot("inProgress", self.entry_id, "active")
        self.tick()
        self.assertEqual(self.state()["history"][0]["outcome"], "resumed")
        self.assertIsNone(self.state()["history"][0]["reactivation_error"])
        self.assertEqual(len(self.codex.delivered), 1)

    def test_uncertain_delivery_requires_human_resolution(self):
        self.codex.lose_reply = True
        self.tick()
        self.tick()
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "needs_attention")
        self.assertEqual(len(self.codex.delivered), 1)

    def test_provisional_desktop_message_is_not_proof_of_delivery(self):
        self.codex.lose_reply = True
        self.tick()
        self.codex.chat = snapshot("failed", self.entry_id, "idle", None)
        self.codex.chat["turnHistory"]["history"]["entitiesByKey"]["latest"]["turnId"] = None
        self.tick()
        self.assertEqual(self.state()["entries"][0]["state"], "needs_attention")
        self.assertEqual(len(self.codex.delivered), 1)

    def test_global_pause_keeps_queue_then_resume_delivers(self):
        with locked_queue(self.path) as queue:
            queue.data["paused"] = True
            queue.save()
        self.assertEqual(self.tick(), ["Monitor is paused"])
        with locked_queue(self.path) as queue:
            queue.data["paused"] = False
            queue.save()
        self.tick()
        self.assertEqual(len(self.codex.delivered), 1)

    def test_corrupt_state_fails_without_overwriting(self):
        self.path.write_text('{"version":999}')
        with self.assertRaisesRegex(ValueError, "Unsupported queue"):
            self.tick()
        self.assertEqual(self.path.read_text(), '{"version":999}')

    def test_ambiguous_bucket_requires_explicit_selection(self):
        raw = usage()
        raw["rateLimitsByLimitId"]["other"] = raw["rateLimitsByLimitId"]["codex"]
        with self.assertRaisesRegex(ValueError, "Choose --limit-id"):
            Usage.parse(raw)
        self.assertEqual(Usage.parse(raw, "other").limit_id, "other")

    def test_monitoring_app_server_rejects_model_execution(self):
        with self.assertRaisesRegex(ValueError, "read-only"):
            AppServer().request("turn/start", {"threadId": "thread-a"})

    def test_connection_failure_marks_waiting_enrollments_and_dry_run_is_read_only(self):
        args = carrie_on.parser().parse_args(["--state", str(self.path), "tick", "--dry-run"])
        before = self.path.read_bytes()
        with patch("carrie_on.AppServer", side_effect=BridgeError("Codex unavailable")):
            with self.assertRaisesRegex(BridgeError, "Codex unavailable"):
                carrie_on.run(args)
            self.assertEqual(self.path.read_bytes(), before)
            args.dry_run = False
            with self.assertRaisesRegex(BridgeError, "Codex unavailable"):
                carrie_on.run(args)
        entry = self.state()["entries"][0]
        self.assertEqual((entry["state"], entry["reactivation_error"]), ("waiting", "Codex unavailable"))
        self.tick()
        self.assertEqual(self.state()["history"][0]["outcome"], "resumed")

    def test_waiting_and_blocked_chats_do_not_count_as_failed_reactivations(self):
        self.codex.limits = usage(100, 100)
        self.tick(99)
        self.assertIsNone(self.state()["entries"][0]["reactivation_error"])
        self.codex.limits = usage(0, 200)
        self.codex.chat["requests"] = [{"kind": "approval"}]
        self.tick()
        self.assertEqual(self.state()["entries"][0]["note"], "Waiting: chat is active or requires attention")
        self.assertIsNone(self.state()["entries"][0]["reactivation_error"])

    def test_existing_queue_without_failure_field_remains_readable(self):
        raw = self.state()
        raw["entries"][0].pop("reactivation_error")
        raw["entries"][0].pop("processing_until")
        self.path.write_text(json.dumps(raw))
        self.tick()
        self.assertEqual(self.state()["history"][0]["outcome"], "resumed")


if __name__ == "__main__":
    unittest.main()
