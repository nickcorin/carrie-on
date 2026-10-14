#!/usr/bin/env python3
"""Loopback API for the native menu card and worker."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import carrie_on
from resume_queue import Usage, UsageObservation, locked_queue
from app_runtime import PROFILE, RUNTIME, VERSION
from app_lifecycle import write_agent


LABEL = PROFILE.labels[1]
PORT = PROFILE.port


class Dashboard:
    """Adapt local UI actions to shared commands and locked queue operations."""

    def __init__(self, state, codex, socket=None):
        self.state = state
        self.codex = codex
        self.socket = socket
        self.build_id = (json.loads((RUNTIME.app / "Contents/Resources/release.json").read_text())["build_id"]
                         if RUNTIME.bundled else None)
        self.usage_observations = {}
        self.usage_observed_at = 0
        self.usage_lock = threading.Lock()
        self.options = ["--state", str(state), "--codex", codex]
        if socket:
            self.options += ["--socket", str(socket)]

    def command(self, *arguments, usage_observation=None):
        return carrie_on.run(carrie_on.parser().parse_args(self.options + list(arguments)),
                              usage_observation=usage_observation)

    def read_usage(self):
        """Keep validated observations from native card reads for enrollment only."""
        observed_at = time.time()
        observations = {}
        try:
            raw = self.command("usage")
            for limit_id in raw.get("rateLimitsByLimitId") or {}:
                observations[limit_id] = UsageObservation(Usage.parse(raw, limit_id), observed_at)
            return raw
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            observations = {}
            raise
        finally:
            with self.usage_lock:
                if observed_at >= self.usage_observed_at:
                    self.usage_observations = observations
                    self.usage_observed_at = observed_at

    def enrollment_usage(self, limit_id):
        with self.usage_lock:
            if limit_id is None and len(self.usage_observations) == 1:
                return next(iter(self.usage_observations.values()))
            return self.usage_observations.get(limit_id)

    def read(self, route, query):
        if route == "/api/health":
            return {"version": VERSION, "build_id": self.build_id}
        if route == "/api/connection":
            return {"connected": self.desktop_connected()}
        if route == "/api/menu":
            return self.menu_summary()
        if route == "/api/status":
            status = self.command("list", "--history")
            status["history"] = list(reversed(status["history"][-30:]))
            status["service_installed"] = self.command("service")["installed"]
            return status
        if route == "/api/chats":
            search = query.get("search", [""])[0]
            if len(search) > 500:
                raise ValueError("Search is too long")
            return self.command("threads", search) if search else self.command("threads")
        if route == "/api/chat-page":
            if query.keys() - {"search", "cursor"} or any(len(values) != 1 for values in query.values()):
                raise ValueError("Expected only a search and cursor")
            search = query.get("search", [""])[0]
            cursor = query.get("cursor", [None])[0]
            if len(search) > 500 or (cursor is not None and len(cursor) > 16384):
                raise ValueError("Search or cursor is too long")
            with carrie_on.AppServer(self.codex) as server:
                page = server.thread_page(search, cursor=cursor)
            return {"chats": [{"id": chat["id"], "title": chat.get("name") or chat.get("preview"),
                               "cwd": chat.get("cwd"), "last_activity": chat.get("updatedAt")}
                              for chat in page["data"]],
                    "next_cursor": page.get("nextCursor")}
        if route == "/api/queued-chats":
            # Read queue membership before contacting Codex; never hold its lock during metadata reads.
            entries = self.command("list")["entries"]
            if not entries:
                return []
            result = []
            with carrie_on.AppServer(self.codex) as server:
                for entry in entries:
                    try:
                        chat = server.request("thread/read", {"threadId": entry["thread_id"], "includeTurns": False})["thread"]
                        result.append({"id": entry["thread_id"], "title": chat.get("name") or entry["title"],
                                       "cwd": chat.get("cwd"), "last_activity": chat.get("updatedAt")})
                    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                        result.append({"id": entry["thread_id"], "title": entry["title"],
                                       "cwd": None, "last_activity": None})
            return result
        if route == "/api/usage":
            return self.read_usage()
        raise ValueError("Unknown local API endpoint")

    def desktop_connected(self):
        """Observe desktop availability without opening a chat or starting a turn."""
        try:
            with carrie_on.Desktop(self.socket, timeout=3):
                return True
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return False

    def menu_summary(self):
        """Return connection, usage, and queue observations in one read-only poll.

        Failed usage or queue reads return null with an error; other observations
        remain usable. The desktop handshake is independent of CLI usage access.
        """
        result = {"connected": self.desktop_connected(), "usage": None, "queue": None,
                  "usage_error": None, "queue_error": None}
        try:
            status = self.command("list")
            result["queue"] = {
                "count": len(status["entries"]), "paused": status["paused"],
                "needs_attention": sum(e["state"] == "needs_attention" for e in status["entries"]),
                "service_installed": self.command("service")["installed"],
                "last_check": status.get("last_check"),
                "last_error": status.get("last_error"),
            }
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            result["queue_error"] = str(error)
        try:
            raw = self.read_usage()
            buckets = raw.get("rateLimitsByLimitId") or {}
            if not buckets:
                raise ValueError("Usage windows unavailable")
            windows = []
            for bucket_id in buckets:
                usage = Usage.parse(raw, bucket_id)
                for window in usage.windows:
                    windows.append({"bucket": bucket_id, "name": window.name,
                                    "used_percent": window.used, "resets_at": window.reset_at,
                                    "window_minutes": buckets[bucket_id][window.name].get("windowDurationMins")})
            result["usage"] = {"windows": windows}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            result["usage_error"] = str(error)
        return result

    def change(self, route, body):
        """Apply an allowlisted local action; see instruction-api.openapi.json for edits."""
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        if route in {"/api/pause", "/api/resume"}:
            return self.command(route.rsplit("/", 1)[1])
        if route not in {"/api/queue", "/api/remove", "/api/instruction"}:
            raise ValueError("Unknown local API action")
        chat = body.get("chat")
        if not isinstance(chat, str):
            raise ValueError("Select a chat first")
        uuid.UUID(chat)
        if route == "/api/remove":
            return self.command("remove", chat)
        prompt = body.get("prompt") if route == "/api/instruction" else body.get("prompt", carrie_on.DEFAULT_PROMPT)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            raise ValueError("Continuation instruction must contain 1–32,000 characters")
        if route == "/api/instruction":
            # This endpoint requires an enrollment UUID, not a reusable chat UUID.
            if set(body) != {"chat", "prompt", "expected_prompt"}:
                raise ValueError("Provide only chat, prompt, and expected_prompt")
            expected = body.get("expected_prompt")
            if not isinstance(expected, str) or not 0 < len(expected) <= 32000:
                raise ValueError("Include the previously saved instruction")
            with locked_queue(self.state) as queue:
                entry = queue.update_instruction(chat, prompt, expected)
                return {"entry": asdict(entry)}
        # Use '=' so user text beginning with '--' remains data to argparse.
        arguments = ["queue", chat, "--prompt=" + prompt]
        limit_id = body.get("limit_id")
        if limit_id is not None:
            if not isinstance(limit_id, str) or not limit_id or len(limit_id) > 200:
                raise ValueError("Invalid usage bucket")
            arguments.append("--limit-id=" + limit_id)
        return self.command(*arguments, usage_observation=self.enrollment_usage(limit_id))


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, dashboard, port=PORT):
        super().__init__(("127.0.0.1", port), Handler)
        self.dashboard = dashboard
        self.origin = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    """Reject foreign origins and DNS rebinding; never expose arbitrary files."""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass

    def respond(self, status, data, content_type="application/json"):
        payload = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self'; connect-src 'self'; img-src 'self' data:; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def trusted_request(self, api=False):
        if self.headers.get("Host") != self.server.origin.removeprefix("http://"):
            return False
        if self.headers.get("Origin") not in (None, self.server.origin):
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site" and api:
            return False
        # A foreign website cannot set this header without a CORS preflight.
        return not api or self.headers.get("X-Carrie-On") == "1"

    def do_GET(self):
        url = urlsplit(self.path)
        if not self.trusted_request(url.path.startswith("/api/")):
            self.respond(403, {"error": "Open Carrie On directly on this Mac"})
            return
        try:
            if url.path in {"/api/health", "/api/connection", "/api/status", "/api/chats", "/api/chat-page", "/api/queued-chats", "/api/usage", "/api/menu"}:
                self.respond(200, self.server.dashboard.read(url.path, parse_qs(url.query)))
            else:
                self.respond(404, {"error": "Page not found"})
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            self.respond(503, {"error": str(error)})

    def do_POST(self):
        if not self.trusted_request(api=True):
            self.respond(403, {"error": "Request must come from the local app"})
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Expected application/json")
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked requests are unsupported")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 150000:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            result = self.server.dashboard.change(urlsplit(self.path).path, body)
            self.respond(200, result)
        except (ValueError, TypeError, KeyError) as error:
            self.respond(400, {"error": str(error)})
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.respond(503, {"error": str(error)})


def install(args):
    """Install the native card's local API service at login."""
    executable = shutil.which(args.codex)
    if not executable:
        raise ValueError("Codex CLI not found")
    logs = RUNTIME.logs
    logs.mkdir(mode=0o700, exist_ok=True)
    command = RUNTIME.command("dashboard") + ["--state", str(args.state.resolve()),
               "--codex", executable, "--port", str(args.port)]
    if args.socket:
        command += ["--socket", str(args.socket.resolve())]
    command += ["serve"]
    environment = {"PATH": f"{Path(executable).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    if os.environ.get("CODEX_HOME"):
        environment["CODEX_HOME"] = os.environ["CODEX_HOME"]
    configuration = {
        "Label": LABEL, "ProgramArguments": command, "WorkingDirectory": str(RUNTIME.data),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "EnvironmentVariables": environment,
        "StandardOutPath": str(logs / "dashboard.log"),
        "StandardErrorPath": str(logs / "dashboard-error.log"),
    }
    path = write_agent(configuration)
    if getattr(args, "start", True):
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    return {"port": args.port}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=RUNTIME.state)
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("command", choices=["serve", "install"])
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be between 1024 and 65535")
    try:
        if args.command == "install":
            print(json.dumps(install(args), indent=2))
        else:
            with DashboardServer(Dashboard(args.state, args.codex, args.socket), args.port) as server:
                server.serve_forever()
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Carrie On local API: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
