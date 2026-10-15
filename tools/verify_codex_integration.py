#!/usr/bin/env python3
"""Check real SDK readiness and optional desktop delivery using a temporary queue.

`wait THREAD` checks an active chat without sending. `continue THREAD` sends a
test instruction to an authorized idle disposable chat and verifies delivery.
`migrate THREAD` also converts an old message anchor through the SDK. Both send
modes require exactly one matching desktop turn and no resend by fresh workers.

All modes use real quota and chat state with a simulated reset baseline in a
temporary queue. They do not test actual quota exhaustion.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from codex_bridge import CodexClient, DesktopClient
from resume_queue import Entry, Usage, check_queue, locked_queue
from verify_desktop import DesktopObserver, history_turns, latest_turn


class QueueDesktop(DesktopClient):
    """Fail if the queue attempts to subscribe to conversation history."""

    def snapshot(self, *_args, **_kwargs):
        raise AssertionError("Queue checks must not fetch a desktop snapshot")


class ObserveDesktop(QueueDesktop):
    """Allow real desktop reads but fail the check if dispatch is attempted."""

    def continue_chat(self, *_):
        raise AssertionError("The active-chat check attempted a continuation")


def verify(mode, thread_id, executable):
    """Run the worker and verify its result. continue and migrate send a test prompt."""
    desktop_type = ObserveDesktop if mode == "wait" else QueueDesktop
    with tempfile.TemporaryDirectory(prefix="carrie-sdk-acceptance-") as directory:
        state = Path(directory) / "queue.json"
        with CodexClient.connect(executable) as server, desktop_type() as desktop, locked_queue(state) as queue:
            chat = server.read_chat(thread_id, desktop, include_goal=True)
            if chat.thread_id != thread_id or chat.busy != (mode == "wait") or chat.blocked:
                raise ValueError("Use an active unblocked chat for wait, or an idle unblocked disposable chat for continue")
            usage = Usage.parse(server.read_usage())
            now = time.time()
            entry = Entry.enroll(chat, usage, "Reply exactly: Carrie On desktop continuation verified.", now)
            if mode == "migrate":
                # Simulate the released enrollment format using real desktop evidence.
                with DesktopObserver() as observer:
                    legacy = latest_turn(observer.snapshot(thread_id))
                assert legacy["turnId"] == chat.turn_id and legacy["params"]["clientUserMessageId"]
                entry.anchor_message_id = legacy["params"]["clientUserMessageId"]
                entry.anchor_turn_id = None
            entry.reset_windows = {name: reset - 1 for name, reset in entry.reset_windows.items()}
            if not entry.reset_ready(usage, now):
                raise ValueError("The account must have available quota and future reset timestamps for this check")
            queue.entries.append(entry)
            queue.save()
            outcomes = check_queue(queue, server, desktop)
            if mode == "wait":
                assert len(queue.entries) == 1 and entry.state == "waiting", outcomes
                assert entry.note == "Waiting: chat is active or requires attention", entry.note
                assert not queue.data["history"] and entry.attempted_at is None
                return {"worker_waited_for_active_chat": True, "dispatch_attempts": 0,
                        "synthetic_reset_baseline": True}

            assert not queue.entries and len(queue.data["history"]) == 1, outcomes
            history = queue.data["history"][0]
            assert history["outcome"] == "resumed" and history["accepted_turn_id"], history["outcome"]
            with DesktopObserver() as observer:
                snapshot = observer.snapshot(thread_id)
            observed = latest_turn(snapshot)
            assert snapshot["id"] == thread_id and observed["turnId"] == history["accepted_turn_id"]
            assert observed["params"]["clientUserMessageId"] == entry.id
            assert history["anchor_turn_id"] == chat.turn_id
        # A fresh worker process must observe the saved outcome and never resend.
        worker = Path(__file__).with_name("carrie_on.py")
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, str(worker), "--state", str(state), "--codex", executable, "--json", "tick"],
                check=True, capture_output=True, text=True, timeout=30,
            )
            assert json.loads(result.stdout) == {"status": "waiting", "due": 0}, result.stdout
        with DesktopObserver() as desktop:
            snapshot = desktop.snapshot(thread_id)
        delivered = [turn for turn in history_turns(snapshot)
                     if turn.get("params", {}).get("clientUserMessageId") == entry.id]
        assert snapshot["id"] == thread_id
        assert len(delivered) == 1 and delivered[0]["turnId"] == history["accepted_turn_id"]
        saved = json.loads(state.read_text())
        assert saved["entries"] == [] and len(saved["history"]) == 1
        return {"thread_id": thread_id, "enrollment_id": entry.id,
                "accepted_turn_id": history["accepted_turn_id"],
                "desktop_accepted_continuation": True, "desktop_message_verified": True,
                "matching_desktop_turns": len(delivered), "fresh_worker_checks_without_resend": 2,
                "persisted_history_records": len(saved["history"]),
                "legacy_anchor_migrated": mode == "migrate", "worker_snapshot_requests": 0,
                "synthetic_reset_baseline": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("wait", "continue", "migrate"))
    parser.add_argument("thread_id", help="An explicitly selected local desktop chat UUID")
    parser.add_argument("--codex", default="codex", help="Installed Codex CLI executable")
    args = parser.parse_args()
    print(json.dumps(verify(args.mode, args.thread_id, args.codex), indent=2))


if __name__ == "__main__":
    main()
