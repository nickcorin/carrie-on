#!/usr/bin/env python3
"""Configure and run the independent Codex continuation monitor."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from codex_bridge import AppServer, Desktop
from resume_queue import Chat, Entry, Usage, check_queue, locked_queue
from app_runtime import RUNTIME, VERSION
from app_lifecycle import remove_agent, write_agent


LABEL = "com.nickcorin.tokenmaxxer"
DEFAULT_PROMPT = "Continue the unfinished work already requested in this chat."


def parser():
    result = argparse.ArgumentParser(
        description="Resume selected Codex chats after quota resets, without model-driven polling.",
        epilog='Example: python3 tokenmaxxer.py queue "Chat title" --prompt "Finish the tests."',
    )
    result.add_argument("--version", action="version", version=f"tokenmaxxer {VERSION}")
    result.add_argument("--state", type=Path, default=RUNTIME.state, help="Queue file")
    result.add_argument("--codex", default=shutil.which("codex") or "codex", help="Codex CLI executable")
    result.add_argument("--socket", type=Path, help="Desktop IPC socket (normally discovered from CODEX_HOME)")
    result.add_argument("--json", action="store_true", help="Machine-readable output")
    commands = result.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Test usage reads and desktop connectivity without inference")
    doctor.add_argument("--thread", help="Also test the live status of a loaded chat ID")
    threads = commands.add_parser("threads", help="Find local chats by title")
    threads.add_argument("search", nargs="?")
    commands.add_parser("usage", help="Read current usage without starting a model turn")
    queue = commands.add_parser("queue", help="Enroll a loaded chat for a single continuation after reset")
    queue.add_argument("chat", help="Exact title, chat UUID, or codex://threads/UUID link")
    prompts = queue.add_mutually_exclusive_group()
    prompts.add_argument("--prompt", default=DEFAULT_PROMPT)
    prompts.add_argument("--prompt-file", help="Read a prompt from a UTF-8 file, or - for stdin")
    queue.add_argument("--limit-id", help="Usage bucket; required when Codex reports several")
    listing = commands.add_parser("list", help="Show queue and monitor status")
    listing.add_argument("--history", action="store_true", help="Include completed/cancelled entries")
    remove = commands.add_parser("remove", help="Cancel an enrollment; leaves the chat untouched")
    remove.add_argument("chat", help="Exact title, chat ID, or enrollment ID")
    commands.add_parser("pause", help="Pause dispatch and preserve all enrollments")
    commands.add_parser("resume", help="Enable dispatch for waiting entries")
    tick = commands.add_parser("tick", help="Check the queue once (used by launchd)")
    tick.add_argument("--dry-run", action="store_true", help="Inspect due chats without sending or updating state")
    install = commands.add_parser("install", help="Install/start the per-user macOS launchd monitor")
    install.add_argument("--interval", type=int, default=60, help="Check interval in seconds (minimum 30)")
    commands.add_parser("uninstall", help="Stop/remove the launchd job; keep queue and history")
    commands.add_parser("service", help="Show launchd service status")
    return result


def resolve_chat(server, query):
    """Accept an exact UUID/link or a unique exact title; never guess a match."""
    if query.startswith("codex://threads/"):
        query = urlparse(query).path.strip("/")
    try:
        uuid.UUID(query)
    except ValueError:
        matches = [t for t in server.threads(query) if (t.get("name") or t.get("preview")) == query]
        if len(matches) != 1:
            raise ValueError(f"Found {len(matches)} exact title matches. Use threads SEARCH, then queue its ID.")
        return matches[0]["id"]
    thread = server.request("thread/read", {"threadId": query, "includeTurns": False})["thread"]
    candidates = server.threads(thread.get("name") or None)
    if not any(t["id"] == query for t in candidates):
        raise ValueError("Chat is archived or absent from the bounded local catalog; open it and retry")
    return query


def usage_summary(raw):
    """Keep output to quota metadata; omit billing and reset-credit details."""
    return {key: raw.get(key) for key in ("accountId", "ordinaryUsageAllowed", "rateLimitsByLimitId")}


def launch_agent(args):
    """Install a user-owned launchd job using absolute paths and a fixed interval."""
    if sys.platform != "darwin":
        raise ValueError("Service installation currently supports macOS only")
    path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    target = f"gui/{os.getuid()}/{LABEL}"
    if args.command == "service":
        result = subprocess.run(["launchctl", "print", target], capture_output=True, text=True)
        return {"installed": result.returncode == 0, "detail": result.stdout or result.stderr}
    if args.command == "uninstall":
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        remove_agent(LABEL)
        return {"message": "Background monitor uninstalled. Queue and history preserved."}
    if args.interval < 30:
        raise ValueError("Use an interval of at least 30 seconds")
    executable = shutil.which(args.codex)
    if executable is None:
        raise ValueError("Codex CLI not found; set --codex to its absolute path")
    log_directory = RUNTIME.logs
    log_directory.mkdir(mode=0o700, exist_ok=True)
    command = RUNTIME.command("worker") + ["--state", str(args.state.resolve()),
               "--codex", executable]
    if args.socket:
        command += ["--socket", str(args.socket.resolve())]
    command += ["tick"]
    environment = {"PATH": f"{Path(executable).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    if os.environ.get("CODEX_HOME"):
        environment["CODEX_HOME"] = os.environ["CODEX_HOME"]
    configuration = {
        "Label": LABEL, "ProgramArguments": command, "WorkingDirectory": str(RUNTIME.data),
        "StartInterval": args.interval, "RunAtLoad": True,
        "ProcessType": "Background", "EnvironmentVariables": environment,
        "StandardOutPath": str(log_directory / "monitor.log"),
        "StandardErrorPath": str(log_directory / "monitor-error.log"),
    }
    path = write_agent(configuration)
    if getattr(args, "start", True):
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    return {"message": f"Background monitor installed; checks every {args.interval} seconds.", "plist": str(path)}


def enroll(args, usage_observation=None):
    """Read enrollment anchors without holding the queue lock, then save atomically.

    A recent server-owned usage observation avoids another quota fetch. Dispatch
    still reads live quota and readiness; no cached observation authorizes a send.
    """
    prompt = args.prompt
    if args.prompt_file:
        prompt = sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).read_text()
    if not prompt.strip() or len(prompt) > 32000:
        raise ValueError("Prompt must contain 1–32,000 characters")
    query = urlparse(args.chat).path.strip("/") if args.chat.startswith("codex://threads/") else args.chat
    try:
        uuid.UUID(query)
        thread_id = query
    except ValueError:
        with AppServer(args.codex) as server:
            thread_id = resolve_chat(server, query)

    with locked_queue(args.state) as queue:
        if any(e.thread_id == thread_id for e in queue.entries):
            raise ValueError("Chat is already queued; remove it before enrolling again")

    def validate_before_open():
        with AppServer(args.codex) as server:
            resolve_chat(server, thread_id)

    with Desktop(args.socket, open_if_needed=True) as desktop:
        chat = Chat.parse(desktop.snapshot(thread_id, before_open=validate_before_open))
    if chat.thread_id != thread_id:
        raise ValueError("Desktop returned a different chat")
    if (usage_observation is not None and usage_observation.usable_for_enrollment(time.time())
            and args.limit_id in (None, usage_observation.usage.limit_id)):
        usage = usage_observation.usage
    else:
        with AppServer(args.codex) as server:
            usage = Usage.parse(server.usage(), args.limit_id)
    entry = Entry.enroll(chat, usage, prompt, time.time())

    with locked_queue(args.state) as queue:
        # Another request may have enrolled this chat while its anchors were read.
        if any(e.thread_id == thread_id for e in queue.entries):
            raise ValueError("Chat is already queued; remove it before enrolling again")
        queue.entries.append(entry)
        queue.save()
        return {"message": f"Queued {entry.title} for a single continuation.",
                "entry": asdict(entry), "paused": queue.data["paused"]}


def run(args, *, usage_observation=None):
    if args.command in {"install", "uninstall", "service"}:
        return launch_agent(args)
    if args.command in {"doctor", "usage", "threads"}:
        with AppServer(args.codex) as server:
            if args.command == "threads":
                return [{"id": t["id"], "title": t.get("name") or t.get("preview"),
                         "cwd": t.get("cwd"), "last_activity": t.get("updatedAt")}
                        for t in server.threads(args.search)]
            raw = server.usage()
            if args.command == "usage":
                return usage_summary(raw)
            with Desktop(args.socket) as desktop:
                report = {"usage_read": "ok", "desktop_ipc": "ok", "model_turns_started": 0,
                          "account_id": raw.get("accountId")}
                if args.thread:
                    chat = Chat.parse(desktop.snapshot(args.thread))
                    report["chat"] = {"title": chat.title, "status": chat.status, "model": chat.model}
                return report

    if args.command == "queue":
        return enroll(args, usage_observation)

    with locked_queue(args.state) as queue:
        if args.command == "list":
            return {"paused": queue.data["paused"], "last_check": queue.data.get("last_check"),
                    "last_error": queue.data.get("last_error"),
                    "entries": [asdict(e) for e in queue.entries],
                    **({"history": queue.data["history"]} if args.history else {})}
        if args.command in {"pause", "resume"}:
            queue.data["paused"] = args.command == "pause"
            queue.save()
            return {"message": f"Monitor {'paused' if queue.data['paused'] else 'enabled'}.",
                    "queued": len(queue.entries)}
        if args.command == "remove":
            matches = [e for e in queue.entries if args.chat in {e.id, e.thread_id, e.title}]
            if len(matches) != 1:
                raise ValueError("Specify an exact, unique queued title or ID")
            queue.finish(matches[0], "cancelled", time.time())
            return {"message": f"Removed {matches[0].title} from the queue."}
        if args.command == "tick":
            due = [e for e in queue.entries if e.state == "sending" or
                   (e.state == "waiting" and time.time() >= max(e.reset_windows.values()))]
            if queue.data["paused"] or not due:
                if not args.dry_run:
                    queue.data["last_check"] = time.time()
                    queue.save()
                return {"status": "paused" if queue.data["paused"] else "waiting", "due": len(due)}
            try:
                with AppServer(args.codex) as server, Desktop(args.socket, open_if_needed=not args.dry_run) as desktop:
                    if args.dry_run:
                        return [{"title": e.title, "state": e.state,
                                 "reset_ready": e.reset_ready(Usage.parse(server.usage(), e.limit_id), time.time()),
                                 "chat_status": Chat.parse(desktop.snapshot(e.thread_id)).status} for e in due]
                    outcomes = check_queue(queue, server, desktop)
                    queue.data["last_error"] = None
                    queue.save()
                    return {"status": "checked", "outcomes": outcomes}
            except (OSError, RuntimeError, ValueError) as error:
                if not args.dry_run:
                    for entry in due:
                        if entry in queue.entries:
                            entry.reactivation_error = str(error) or type(error).__name__
                    queue.data["last_check"] = time.time()
                    queue.data["last_error"] = str(error)
                    queue.save()
                raise


def main():
    args = parser().parse_args()
    try:
        output = run(args)
        # Empty checks stay silent in launchd logs; status remains in the queue file.
        if args.command == "tick" and not args.json and isinstance(output, dict):
            for outcome in output.get("outcomes", []):
                print(outcome)
        elif args.json:
            print(json.dumps(output, indent=2))
        elif isinstance(output, dict) and "message" in output:
            print(output["message"])
            if output.get("paused"):
                print("Dispatch is paused. Run: python3 tokenmaxxer.py resume")
        else:
            print(json.dumps(output, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"tokenmaxxer: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
