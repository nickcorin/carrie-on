#!/usr/bin/env python3
"""Compare released and current CLIs against the same oversized desktop history.

Uses a real framed Unix socket and a fake Codex executable with compact SDK
responses. No installed app, actual conversation, or production queue is used.
The fixture includes a 33 MiB historical user message, preserving the latest
message/turn anchor. The baseline source comes from the local v0.5.6 Git tag.
"""

import argparse
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from test_desktop_protocol import DesktopProtocol
from test_sdk_protocol import SDKProtocol


ROOT = Path(__file__).resolve().parent


def command(source):
    """Run a source checkout with this interpreter, or an already bundled CLI."""
    return [sys.executable, str(source / "carrie_on.py")] if source.is_dir() else [str(source)]


def run_cli(source, codex, state, arguments, *, large):
    fixture = DesktopProtocol()
    fixture.setUp()
    fixture.allow_snapshot_disconnect = True
    if large:
        fixture.snapshot_data["turnHistory"]["history"]["entitiesByKey"]["earlier"] = {
            "turnId": "older-turn", "status": "completed",
            "params": {"clientUserMessageId": "older-message",
                       "input": [{"type": "text", "text": "x" * (33 * 1024 * 1024)}]},
        }
    # Desktop and SDK expose different IDs for the same latest accepted turn.
    fixture.snapshot_data["turnHistory"]["history"]["entitiesByKey"]["latest"]["turnId"] = "turn-a"
    fixture.worker.start()
    try:
        result = subprocess.run(
            [*command(source), "--state", str(state),
             "--codex", str(codex), "--socket", str(fixture.path), "--json", *arguments],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        fixture.finish()
        return {"exit_code": result.returncode,
                "result": json.loads(result.stdout) if result.returncode == 0 else result.stderr.strip(),
                "snapshot_requests": fixture.snapshot_requests, "snapshot_bytes": fixture.snapshot_bytes,
                "desktop_sends": len(fixture.requests)}, json.loads(state.read_text()) if state.exists() else None
    finally:
        fixture.doCleanups()


def lower_reset_baselines(state_path):
    """Subtract a second from each saved reset time."""
    data = json.loads(state_path.read_text())
    for entry in data["entries"]:
        entry["reset_windows"] = {name: reset - 1 for name, reset in entry["reset_windows"].items()}
    state_path.write_text(json.dumps(data))


def verify(current=ROOT):
    sdk = SDKProtocol()
    sdk.setUp()
    try:
        sdk.fixture("quota")
        text = sdk.executable.read_text().replace('import json, sys', 'import json, sys, time')
        sdk.executable.write_text(text.replace('"resetsAt":200', '"resetsAt":int(time.time())+3600'))
        with tempfile.TemporaryDirectory(prefix="carrie-large-chat-") as directory:
            directory = Path(directory)
            baseline = directory / "v0.5.6"
            baseline.mkdir()
            archive = subprocess.check_output(["git", "archive", "v0.5.6"], cwd=ROOT)
            subprocess.run(["tar", "-x", "-C", str(baseline)], input=archive, check=True)
            state = directory / "queue.json"
            report = {"baseline_commit": subprocess.check_output(
                ["git", "rev-parse", "v0.5.6^{commit}"], cwd=ROOT, text=True).strip()}
            report["released_enrollment"], _ = run_cli(baseline, sdk.executable, state, ["queue", "Finish feature"], large=False)
            assert report["released_enrollment"]["exit_code"] == 0, report
            lower_reset_baselines(state)
            report["released_large_chat_tick"], failed = run_cli(baseline, sdk.executable, state, ["tick"], large=True)
            assert failed["entries"][0]["reactivation_error"] == "Invalid or oversized Codex desktop frame", report
            original = copy.deepcopy(failed)
            sdk.request_log.write_text("")
            report["existing_entry_after_upgrade"], upgraded = run_cli(current, sdk.executable, state, ["tick"], large=True)
            report["migration_sdk_reads"] = [json.loads(line) for line in sdk.request_log.read_text().splitlines()
                                             if json.loads(line)["method"] in {"thread/turns/list", "thread/items/list"}]
            report["existing_entry_outcome"] = (upgraded["history"][-1]["outcome"] if upgraded["history"]
                                                 else upgraded["entries"][0]["reactivation_error"])
            report["same_entry_id_preserved"] = (original["entries"][0]["id"] ==
                (upgraded["history"][-1]["id"] if upgraded["history"] else upgraded["entries"][0]["id"]))
            assert report["existing_entry_outcome"] == "resumed", report
            assert report["same_entry_id_preserved"], report
            assert report["existing_entry_after_upgrade"]["desktop_sends"] == 1, report
            assert report["existing_entry_after_upgrade"]["snapshot_requests"] == 0, report
            assert upgraded["history"][0]["anchor_turn_id"] == "turn-a", report
            assert upgraded["history"][0]["prompt"] == original["entries"][0]["prompt"], report
            retry = subprocess.run(
                [*command(current), "--state", str(state), "--json", "tick"],
                cwd=ROOT, capture_output=True, text=True, check=True, timeout=10,
            )
            report["existing_entry_fresh_worker_check"] = json.loads(retry.stdout)
            assert report["existing_entry_fresh_worker_check"] == {"status":"waiting", "due":0}, report
            new_state = directory / "new-queue.json"
            report["new_enrollment_large_chat"], _ = run_cli(current, sdk.executable, new_state, ["queue", "Finish feature"], large=True)
            assert report["new_enrollment_large_chat"]["exit_code"] == 0, report
            lower_reset_baselines(new_state)
            report["new_entry_large_chat_tick"], fresh = run_cli(current, sdk.executable, new_state, ["tick"], large=True)
            assert fresh["history"][0]["outcome"] == "resumed", report
            assert report["new_entry_large_chat_tick"]["desktop_sends"] == 1, report
            assert report["new_entry_large_chat_tick"]["snapshot_requests"] == 0, report
            return report
    finally:
        sdk.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-cli", type=Path, default=ROOT,
                        help="Current source checkout or extracted Dev CLI to verify")
    print(json.dumps(verify(parser.parse_args().current_cli), indent=2))
