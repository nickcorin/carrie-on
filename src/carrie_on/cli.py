#!/usr/bin/env python3
"""Human-friendly and scriptable HTTP client for Carrie On."""

import json
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse
from uuid import UUID, uuid4

import typer

from carrie_on.macos.lifecycle import control_service
from carrie_on.macos.runtime import PROFILES, BuildProfile, Runtime

app = typer.Typer(
    no_args_is_help=True,
    help="Queue Codex instructions after usage resets.",
    context_settings={"help_option_names": ["-h", "--help"]},
    epilog="Examples: carrie-on queue add 'Chat title'; carrie-on --json queue list. "
    "Guide: https://github.com/nickcorin/carrie-on/blob/master/USAGE.md",
)
queue = typer.Typer(no_args_is_help=True, help="Manage queued instructions and delivery history.")
chats = typer.Typer(no_args_is_help=True, help="Find local Codex chats.")
service = typer.Typer(
    no_args_is_help=True, help="Manage the local backend without changing queued work."
)
app.add_typer(queue, name="queue")
app.add_typer(chats, name="chats")
app.add_typer(service, name="service")


class ClientError(RuntimeError):
    def __init__(self, detail: str, exit_code: int = 5):
        super().__init__(detail)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Client:
    url: str
    timeout: float
    json_output: bool
    profile: BuildProfile = PROFILES["release"]

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        params: dict[str, object] | None = None,
        etag: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Return a JSON response and ETag; transport errors leave mutations uncertain."""
        url = self.url.rstrip("/") + "/api/v1/" + path.lstrip("/")
        if params:
            url += "?" + urlencode(
                {key: value for key, value in params.items() if value is not None}
            )
        headers = {"X-Carrie-On": "1", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = (
                "application/merge-patch+json" if method == "PATCH" else "application/json"
            )
        if etag:
            headers["If-Match"] = etag
        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        # Local traffic must never inherit an HTTP proxy or follow a redirect.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                value = json.load(response)
                if not isinstance(value, dict):
                    raise ValueError("The service returned an invalid response")
                return value, response.headers.get("ETag")
        except urllib.error.HTTPError as error:
            try:
                detail = json.load(error).get("detail", str(error))
            except (ValueError, AttributeError):
                detail = str(error)
            code = {404: 3, 409: 4, 412: 4, 428: 4, 422: 2, 400: 2}.get(error.code, 5)
            raise ClientError(detail, code) from error
        except (OSError, ValueError) as error:
            raise ClientError(f"Local service unavailable: {error}") from error

    def show(self, value: dict[str, Any]) -> None:
        if self.json_output:
            typer.echo(json.dumps(value, indent=2))
        elif isinstance(value, dict) and "items" in value:
            for item in value["items"]:
                state = item.get("state") or item.get("outcome") or ""
                typer.echo(
                    f"{item['id']}  {state:<10} {item.get('title') or item.get('reason') or ''}"
                )
                detail = item.get("error") or item.get("reason")
                if item.get("title") and detail:
                    typer.echo("  " + detail)
            if value.get("next_cursor"):
                typer.echo("More results: use --cursor " + value["next_cursor"], err=True)
        elif isinstance(value, dict) and "instruction" in value:
            typer.echo(f"{value['id']}  {value['state']}  {value['title']}")
            typer.echo(value["instruction"])
            if value.get("error"):
                typer.echo(value["error"])
        else:
            typer.echo(json.dumps(value, indent=2))


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"carrie-on {Runtime.current().version}")
        raise typer.Exit()


@app.callback()
def options(
    ctx: typer.Context,
    server: Optional[str] = typer.Option(
        None, envvar="CARRIE_ON_SERVER", help="Loopback server URL."
    ),
    profile: Optional[str] = typer.Option(
        None,
        envvar="CARRIE_ON_PROFILE",
        help="release or dev; defaults to this app’s profile.",
    ),
    timeout: float = typer.Option(60, min=0.1, max=300, envvar="CARRIE_ON_TIMEOUT"),
    json_output: bool = typer.Option(False, "--json", help="Write structured JSON to stdout."),
    version: bool = typer.Option(False, "--version", callback=version_callback, is_eager=True),
):
    profile = profile or Runtime.current().profile.name
    if profile not in PROFILES:
        raise typer.BadParameter("Choose release or dev", param_hint="--profile")
    settings = PROFILES[profile].data_path(Path.home()) / "settings.json"
    saved = json.loads(settings.read_text()) if server is None and settings.exists() else {}
    if not isinstance(saved, dict):
        raise ValueError("settings.json must contain a JSON object")
    url = server or f"http://127.0.0.1:{saved.get('port', PROFILES[profile].port)}"
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as error:
        raise typer.BadParameter("Use http://127.0.0.1:PORT", param_hint="--server") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.path not in ("", "/")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise typer.BadParameter("Use http://127.0.0.1:PORT", param_hint="--server")
    if port is None or not 1024 <= port <= 65535:
        raise typer.BadParameter("Use a port between 1024 and 65535", param_hint="--server")
    ctx.obj = Client(url, timeout, json_output, PROFILES[profile])


def read_instruction(text: str | None, file: str | None, *, required: bool = False) -> str | None:
    if text is not None and file is not None:
        raise typer.BadParameter("Choose --instruction or --instruction-file")
    if file is not None:
        text = sys.stdin.read() if file == "-" else Path(file).read_text()
    if text is None and required:
        raise typer.BadParameter("Provide --instruction or --instruction-file")
    if text is not None and (not text.strip() or len(text) > 32000):
        raise typer.BadParameter("Instruction must contain 1–32,000 characters")
    return text


@app.command()
def status(ctx: typer.Context):
    """Show connection, usage, and queue status."""
    ctx.obj.show(ctx.obj.request("GET", "status")[0])


@app.command()
def usage(ctx: typer.Context):
    """Read usage without starting a model turn."""
    ctx.obj.show(ctx.obj.request("GET", "usage")[0])


@app.command()
def doctor(ctx: typer.Context, chat: Optional[UUID] = typer.Option(None, "--chat", "--thread")):
    """Check usage and desktop connectivity without sending or opening chats."""
    ctx.obj.show(ctx.obj.request("GET", "diagnostics", params={"chat_id": chat})[0])


@chats.command("list")
def list_chats(
    ctx: typer.Context,
    search: str = "",
    limit: int = typer.Option(25, min=1, max=100),
    cursor: Optional[str] = None,
):
    """Browse recent chats, with optional title search."""
    ctx.obj.show(
        ctx.obj.request(
            "GET", "chats", params={"search": search, "limit": limit, "cursor": cursor}
        )[0]
    )


@queue.command("add")
def add(
    ctx: typer.Context,
    chat: str,
    instruction: Optional[str] = typer.Option(None),
    instruction_file: Optional[str] = typer.Option(None),
    limit_id: Optional[str] = None,
    id: Optional[UUID] = typer.Option(None, help="Stable UUID for safe retries."),
):
    """Queue an exact title, chat UUID, or codex://threads/UUID link."""
    text = read_instruction(instruction, instruction_file)
    item_id = str(id or uuid4())
    chat_id = ctx.obj.request("GET", "chats/resolve", params={"query": chat})[0]["id"]
    body = {"id": item_id, "chat_id": chat_id, "limit_id": limit_id}
    if text is not None:
        body["instruction"] = text
    typer.echo(f"Queue item ID: {item_id}", err=True)
    try:
        ctx.obj.show(ctx.obj.request("POST", "queue", body)[0])
    except ClientError as error:
        if error.exit_code == 5:
            typer.echo(
                f"Creation outcome is unknown. Inspect queue item {item_id}, or retry the same input with --id {item_id}.",
                err=True,
            )
        raise
    except (KeyboardInterrupt, typer.Exit):
        typer.echo(
            f"Creation interrupted; its outcome is unknown. Inspect queue item {item_id}, or retry the same input with --id {item_id}.",
            err=True,
        )
        raise


@queue.command("list")
def list_queue(
    ctx: typer.Context,
    state: str = "open",
    history: bool = False,
    limit: int = typer.Option(50, min=1, max=100),
    cursor: Optional[str] = None,
):
    """List outstanding items; --history includes every retained outcome."""
    ctx.obj.show(
        ctx.obj.request(
            "GET",
            "queue",
            params={
                "state": "all" if history else state,
                "limit": limit,
                "cursor": cursor,
            },
        )[0]
    )


@queue.command("show")
def show(ctx: typer.Context, item_id: UUID):
    """Inspect an item by its stable UUID."""
    ctx.obj.show(ctx.obj.request("GET", f"queue/{item_id}")[0])


@queue.command("edit")
def edit(
    ctx: typer.Context,
    item_id: UUID,
    instruction: Optional[str] = None,
    instruction_file: Optional[str] = None,
):
    """Change an unclaimed queued instruction using a version precondition."""
    text = read_instruction(instruction, instruction_file, required=True)
    _, etag = ctx.obj.request("GET", f"queue/{item_id}")
    ctx.obj.show(ctx.obj.request("PATCH", f"queue/{item_id}", {"instruction": text}, etag=etag)[0])


@queue.command("cancel")
def cancel(ctx: typer.Context, item_id: UUID):
    """Remove an item from the queue, retaining its history. Repetition is safe."""
    ctx.obj.show(ctx.obj.request("POST", f"queue/{item_id}/cancel", {})[0])


@queue.command("check")
def check(
    ctx: typer.Context,
    limit: int = typer.Option(50, min=1, max=100),
    cursor: Optional[str] = None,
):
    """Inspect readiness without claiming, changing state, opening chats, or sending."""
    ctx.obj.show(
        ctx.obj.request("GET", "queue/check", params={"limit": limit, "cursor": cursor})[0]
    )


@service.command("status")
def service_status(ctx: typer.Context):
    ctx.obj.show(control_service("status", ctx.obj.profile))


@service.command()
def start(ctx: typer.Context):
    """Start the installed backend; repeated starts are safe."""
    ctx.obj.show(control_service("start", ctx.obj.profile))


@service.command()
def stop(ctx: typer.Context):
    """Stop the backend while preserving queue and history."""
    ctx.obj.show(control_service("stop", ctx.obj.profile))


@service.command()
def uninstall(ctx: typer.Context):
    """Remove backend startup registration, preserving all user data."""
    ctx.obj.show(control_service("uninstall", ctx.obj.profile))


@service.command()
def install(
    ctx: typer.Context,
    codex: Optional[str] = None,
    port: Optional[int] = typer.Option(None, min=1024, max=65535),
    interval: Optional[int] = typer.Option(None, min=30),
    socket: Optional[Path] = None,
):
    """Install a backend for the selected profile; preserve queue data."""
    from carrie_on.macos.installation import SettingsOverrides, install_service

    runtime = Runtime.current(ctx.obj.profile, registered=True)
    overrides = SettingsOverrides(
        codex=codex,
        port=port or urlparse(ctx.obj.url).port,
        interval=interval,
        socket=socket,
    )
    ctx.obj.show(install_service(runtime, overrides))


def main() -> int:
    def interrupted(*_):
        raise typer.Exit(130)

    previous_interrupt = signal.signal(signal.SIGINT, interrupted)
    try:
        app()
        return 0
    except typer.Exit as error:
        return error.exit_code
    except ClientError as error:
        typer.echo(f"carrie-on: {error}", err=True)
        return error.exit_code
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        typer.echo(f"carrie-on: {error}", err=True)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_interrupt)


if __name__ == "__main__":
    sys.exit(main())
