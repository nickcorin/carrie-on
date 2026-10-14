"""Persistent, single-use continuation queue and reset eligibility rules."""

import contextlib
import fcntl
import json
import math
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Window:
    """A reported usage window; reset_at is Unix time in seconds."""

    name: str
    used: float
    reset_at: int


@dataclass(frozen=True)
class Usage:
    """Validated quota for an account and explicitly selected limit bucket."""

    account_id: str
    limit_id: str
    windows: tuple[Window, ...]
    allowed: bool

    @classmethod
    def parse(cls, response, limit_id=None):
        """Reject missing/ambiguous quota instead of inferring availability."""
        if not isinstance(response, dict):
            raise ValueError("Invalid quota response")
        account_id = response.get("accountId")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("Codex did not report an account ID")
        buckets = response.get("rateLimitsByLimitId")
        if buckets is None:
            legacy = response.get("rateLimits")
            buckets = {legacy.get("limitId") or "codex": legacy} if legacy else {}
        if not isinstance(buckets, dict) or not buckets:
            raise ValueError("Codex did not report usage buckets")
        if limit_id is None:
            if len(buckets) != 1:
                raise ValueError("Choose --limit-id from: " + ", ".join(buckets))
            limit_id = next(iter(buckets))
        if limit_id not in buckets:
            raise ValueError(f"Usage bucket {limit_id!r} is unavailable")
        bucket = buckets[limit_id]
        if not isinstance(bucket, dict):
            raise ValueError("Invalid usage bucket")
        windows = []
        for name in ("primary", "secondary"):
            raw = bucket.get(name)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                raise ValueError(f"Invalid {name} quota window")
            used, reset = raw.get("usedPercent"), raw.get("resetsAt")
            if (type(used) not in (int, float) or not math.isfinite(used) or used < 0
                    or type(reset) is not int or reset <= 0):
                raise ValueError(f"Incomplete {name} quota information")
            windows.append(Window(name, used, reset))
        if not windows:
            raise ValueError("No timed usage windows available")
        ordinary = response.get("ordinaryUsageAllowed")
        if type(ordinary) is not bool:
            raise ValueError("Missing or invalid usage availability flag")
        allowed = (ordinary and bucket.get("spendControlReached") is not True
                   and not bucket.get("rateLimitReachedType"))
        return cls(account_id, limit_id, tuple(windows), allowed)


@dataclass(frozen=True)
class UsageObservation:
    """Recent quota may anchor enrollment, but never authorize dispatch."""

    usage: Usage
    observed_at: float

    def usable_for_enrollment(self, now):
        return (0 <= now - self.observed_at <= 60
                and all(window.reset_at > now for window in self.usage.windows))


@dataclass(frozen=True)
class Chat:
    """Live desktop state relevant to continuing an enrolled chat."""

    thread_id: str
    title: str
    message_id: str
    model: str
    status: str
    busy: bool
    blocked: bool
    quota_failed: bool
    seen_message_ids: frozenset[str]
    turn_id: str | None = None

    @classmethod
    def from_headers(cls, metadata, turns, settings, goal):
        """Interpret persisted turn completion, never process-local idle status.

        Missing completion evidence means still busy or unknown. A synthetic
        interrupted status from an unloaded app-server cannot authorize a send.
        Approval requests keep their turn unfinished; their bodies are not read.
        """
        if len(turns) != 1:
            raise ValueError("Chat has no identifiable latest turn; open it in Codex first")
        turn = turns[0]
        thread_id, turn_id, model = metadata.get("id"), turn.get("id"), settings.get("model")
        if not all(isinstance(value, str) and value for value in (thread_id, turn_id, model)):
            raise ValueError("Chat has no identifiable latest turn or model")
        status = turn.get("status")
        if status not in {"inProgress", "completed", "failed", "interrupted"}:
            raise ValueError("Unsupported Codex turn status")
        completed = turn.get("completedAt")
        busy = status == "inProgress" or type(completed) is not int or completed <= 0
        blocked = (settings.get("resumeState") != "resumed"
                   or (goal or {}).get("status") in {"paused", "budgetLimited", "blocked"})
        return cls(thread_id, metadata.get("name") or thread_id, turn_id, model, status,
                   busy, blocked, (turn.get("error") or {}).get("codexErrorInfo") == "usageLimitExceeded",
                   frozenset(), turn_id)

    @classmethod
    def parse(cls, snapshot):
        """Read the latest turn from either supported desktop history shape."""
        if not isinstance(snapshot, dict):
            raise ValueError("Invalid desktop snapshot")
        thread_id = snapshot.get("id")
        message_id = snapshot.get("latestTurnStartMessageId")
        model = snapshot.get("latestModel")
        if not all(isinstance(v, str) and v for v in (thread_id, message_id, model)):
            raise ValueError("Chat has no identifiable latest turn or model; open it in Codex first")
        turns = snapshot.get("turns", [])
        history = snapshot.get("turnHistory") or {}
        if history.get("kind") == "canonical":
            turns = list(history["history"]["entitiesByKey"].values())
        # The desktop can show a provisional user message before server acceptance.
        ids = frozenset(t.get("params", {}).get("clientUserMessageId") for t in turns if t.get("turnId"))
        latest = [t for t in turns if t.get("params", {}).get("clientUserMessageId") == message_id]
        if len(latest) != 1:
            raise ValueError("Cannot identify the latest desktop turn; refusing to guess")
        turn = latest[0]
        status = turn.get("status")
        if status not in {"inProgress", "completed", "failed", "interrupted"}:
            raise ValueError(f"Unsupported turn status: {status!r}")
        error = turn.get("error") or {}
        quota_failed = error.get("codexErrorInfo") == "usageLimitExceeded"
        runtime = snapshot.get("threadRuntimeStatus") or {}
        goal = snapshot.get("threadGoal") or {}
        blocked = (bool(snapshot.get("requests")) or bool(snapshot.get("externalRequests"))
                   or goal.get("status") in {"paused", "budgetLimited", "blocked"}
                   or runtime.get("type") not in {"active", "idle", "systemError"}
                   or snapshot.get("resumeState") != "resumed")
        return cls(thread_id, snapshot.get("title") or thread_id, message_id, model, status,
                   runtime.get("type") == "active" or status == "inProgress",
                   blocked, quota_failed, ids, turn.get("turnId"))


@dataclass
class Entry:
    """An enrollment permits a single accepted continuation after reset."""

    id: str
    thread_id: str
    title: str
    account_id: str
    limit_id: str
    anchor_message_id: str
    model: str
    prompt: str
    reset_windows: dict[str, int]
    enrolled_while_busy: bool
    state: str = "waiting"
    note: str = "Waiting for the recorded usage reset"
    attempted_at: float | None = None
    reactivation_error: str | None = None
    processing_until: float | None = None
    anchor_turn_id: str | None = None
    accepted_turn_id: str | None = None

    @classmethod
    def enroll(cls, chat, usage, prompt, now):
        """Capture task/reset anchors; current readiness is checked at dispatch."""
        if not prompt.strip():
            raise ValueError("Continuation prompt cannot be empty")
        exhausted = [w for w in usage.windows if w.used >= 100]
        future = [w for w in usage.windows if w.reset_at > now]
        if not exhausted and not future:
            raise ValueError("Usage timestamps are stale; try again after Codex refreshes them")
        selected = exhausted or [min(future, key=lambda w: w.reset_at)]
        return cls(str(uuid.uuid4()), chat.thread_id, chat.title, usage.account_id,
                   usage.limit_id, chat.message_id, chat.model, prompt,
                   {w.name: w.reset_at for w in selected}, chat.busy, anchor_turn_id=chat.turn_id)

    @classmethod
    def parse(cls, raw):
        """Validate the persisted queue boundary, including send recovery state."""
        entry = cls(**raw)
        uuid.UUID(entry.id)
        for value in (entry.thread_id, entry.title, entry.account_id, entry.limit_id,
                      entry.anchor_message_id, entry.model, entry.prompt):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Invalid queue entry text field")
        if entry.state not in {"waiting", "sending", "needs_attention"}:
            raise ValueError("Invalid queue entry state")
        if entry.state == "sending" and (type(entry.attempted_at) not in (int, float)
                                          or not math.isfinite(entry.attempted_at)):
            raise ValueError("Sending entry has no valid attempt timestamp")
        if not isinstance(entry.reset_windows, dict) or not entry.reset_windows or any(
            k not in {"primary", "secondary"} or type(v) is not int or v <= 0
            for k, v in entry.reset_windows.items()
        ):
            raise ValueError("Invalid reset windows")
        if type(entry.enrolled_while_busy) is not bool:
            raise ValueError("Invalid enrollment state")
        for identifier in (entry.anchor_turn_id, entry.accepted_turn_id):
            if identifier is not None and (not isinstance(identifier, str) or not identifier):
                raise ValueError("Invalid Codex turn identifier")
        if entry.reactivation_error is not None and (
                not isinstance(entry.reactivation_error, str) or not entry.reactivation_error.strip()):
            raise ValueError("Invalid reactivation error")
        if entry.processing_until is not None and (
                type(entry.processing_until) not in (int, float) or not math.isfinite(entry.processing_until)):
            raise ValueError("Invalid processing expiry")
        return entry

    def require_attention(self, reason):
        """Hold an unsuccessful reactivation for review without retrying delivery."""
        self.state = "needs_attention"
        self.note = reason
        self.reactivation_error = reason

    def reset_ready(self, usage, now):
        """Require a fresh, advanced reset and headroom in every reported window."""
        if usage.account_id != self.account_id or usage.limit_id != self.limit_id:
            raise ValueError("Codex account or quota bucket changed")
        if not usage.allowed or any(w.used >= 100 for w in usage.windows):
            return False
        windows = {w.name: w for w in usage.windows}
        return all(name in windows and windows[name].reset_at > max(baseline, now)
                   for name, baseline in self.reset_windows.items())


class Queue:
    """Atomically persisted queue. Hold locked_queue for every mutation."""

    def __init__(self, path):
        self.path = Path(path)
        raw = json.loads(self.path.read_text()) if self.path.exists() else {
            "version": 2, "paused": False, "entries": [], "history": [],
        }
        if raw.get("version") == 1:
            if raw.get("entries"):
                raise ValueError("Re-enroll legacy entries before migrating to the independent monitor")
            raw["version"] = 2
            raw["paused"] = False
            raw["retired_automation_id"] = raw.pop("automationId", None)
        if raw.get("version") != 2 or type(raw.get("paused")) is not bool:
            raise ValueError("Unsupported queue file format")
        self.data = raw
        if not isinstance(raw.get("entries"), list) or not isinstance(raw.get("history"), list):
            raise ValueError("Invalid queue entries or history")
        self.entries = [Entry.parse(e) for e in raw["entries"]]
        if len({e.thread_id for e in self.entries}) != len(self.entries):
            raise ValueError("Duplicate chat enrollment in queue")

    def save(self):
        """Flush state before atomic replacement; a crash cannot truncate the queue."""
        self.data["entries"] = [asdict(e) for e in self.entries]
        fd, temporary = tempfile.mkstemp(prefix=".resume-queue-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.data, stream, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def finish(self, entry, outcome, now):
        """Remove an enrollment and retain its observable outcome in history."""
        entry.processing_until = None
        self.entries.remove(entry)
        self.data["history"].append({**asdict(entry), "outcome": outcome, "finished_at": now})
        self.save()

    def update_instruction(self, enrollment_id, prompt, expected_prompt):
        """Save a waiting enrollment's instruction while holding locked_queue.

        The caller validates instruction strings. Match the exact enrollment ID
        and previously read prompt; preserve the deadline and all dispatch state.
        An identical retry succeeds. Missing, changed, or non-waiting entries
        raise ValueError without writing state.
        """
        entry = next((e for e in self.entries if e.id == enrollment_id), None)
        if entry is None:
            raise ValueError("This enrollment is no longer queued. Reopen the card to refresh.")
        if entry.state != "waiting":
            raise ValueError("This chat has a send in progress or needs attention; its instruction cannot be changed.")
        if entry.prompt == prompt:
            return entry
        if entry.prompt != expected_prompt:
            raise ValueError("The instruction changed elsewhere. Reopen the card before saving again.")
        entry.prompt = prompt
        self.save()
        return entry


@contextlib.contextmanager
def locked_queue(path):
    """Serialize CLI and worker access with a kernel lock released on process exit."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Queue is busy; another command is checking it. Try again shortly.")
                time.sleep(0.05)
        yield Queue(path)


def check_queue(queue, server, desktop, now=None):
    """Check reset eligibility; never retry an uncertain send automatically.

    The SDK reads headers; desktop dispatch retains settings, tools and approvals.
    No dependency evaluates a prompt to choose an action.
    """
    now = time.time() if now is None else now
    outcomes = []
    if queue.data["paused"]:
        return ["Monitor is paused"]
    for entry in list(queue.entries):
        if entry.state == "needs_attention":
            continue
        try:
            entry.reactivation_error = None
            # Presentation lease only; a crashed worker must not animate forever.
            entry.processing_until = now + 120 if entry.state == "sending" else None
            if entry.state == "sending":
                queue.save()
            if entry.state == "waiting":
                usage = Usage.parse(server.usage(), entry.limit_id)
                if not entry.reset_ready(usage, now):
                    entry.note = "Waiting for fresh reset confirmation and available quota"
                    continue
                if not any(t["id"] == entry.thread_id for t in server.threads(entry.title)):
                    entry.require_attention("Chat is archived, renamed, or absent from the local catalog")
                    continue
            if entry.state == "sending":
                confirmed = entry.accepted_turn_id is not None
                if not confirmed and entry.anchor_turn_id is None:
                    legacy = Chat.parse(desktop.snapshot(entry.thread_id))
                    if legacy.thread_id != entry.thread_id:
                        raise ValueError("Desktop returned a different chat")
                    confirmed = entry.id in legacy.seen_message_ids
                if confirmed:
                    queue.finish(entry, "resumed", now)
                    outcomes.append(f"Confirmed continuation: {entry.title}")
                else:
                    entry.require_attention("Delivery uncertain; inspect the chat before removing and re-enrolling")
                continue
            if entry.anchor_turn_id is None:
                legacy = Chat.parse(desktop.snapshot(entry.thread_id))
                if legacy.thread_id != entry.thread_id or legacy.message_id != entry.anchor_message_id:
                    entry.require_attention("Chat changed after enrollment; remove and re-enroll if still wanted")
                    continue
                if not legacy.turn_id:
                    raise ValueError("The enrolled task has no accepted Codex turn ID")
                entry.anchor_turn_id = legacy.turn_id
                queue.save()
            chat = server.read_chat(entry.thread_id, desktop, include_goal=True)
            if chat.thread_id != entry.thread_id:
                raise ValueError("Codex returned a different chat")
            if chat.turn_id != entry.anchor_turn_id or chat.model != entry.model:
                entry.require_attention("Chat changed after enrollment; remove and re-enroll if still wanted")
                continue
            if chat.busy or chat.blocked:
                entry.note = "Waiting: chat is active or requires attention"
                continue
            if entry.enrolled_while_busy and chat.status == "completed":
                queue.finish(entry, "finished_before_reset", now)
                outcomes.append(f"Already finished: {entry.title}")
                continue
            if entry.enrolled_while_busy and not chat.quota_failed:
                entry.require_attention("Chat stopped for a reason other than quota exhaustion")
                continue

            # Persist before crossing the send boundary; a lost reply is not a retry signal.
            entry.state = "sending"
            entry.attempted_at = now
            entry.processing_until = now + 120
            entry.note = "Continuation dispatch started"
            queue.save()
            receipt = desktop.continue_chat(entry.thread_id, entry.prompt, entry.id)
            entry.accepted_turn_id = receipt["turn_id"]
            queue.save()
            queue.finish(entry, "resumed", now)
            outcomes.append(f"Continuation accepted: {entry.title}")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            entry.note = str(error) or type(error).__name__
            entry.reactivation_error = entry.note
            if isinstance(error, ValueError):
                entry.state = "needs_attention"
            outcomes.append(f"{entry.title}: {entry.note}")
        finally:
            entry.processing_until = None
            queue.save()
    queue.data["last_check"] = now
    queue.save()
    return outcomes
