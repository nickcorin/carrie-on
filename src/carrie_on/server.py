"""Configure and run the local HTTP service and dispatch worker."""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from importlib.metadata import version as package_version
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from carrie_on.api import create_app
from carrie_on.application import QueueService
from carrie_on.codex.client import CodexConnections
from carrie_on.dispatch import DispatchWorker
from carrie_on.legacy import LegacyImporter
from carrie_on.ports import QueueStorage, StorageMaintenance, StorageUnavailable
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance


def create_application(
    store: QueueStorage,
    connections: CodexConnections,
    *,
    maintenance: StorageMaintenance,
    legacy_path: Path | None = None,
    port: int = 8766,
    version: str | None = None,
    build_id: str | None = None,
    interval: float = 60,
    run_worker: bool = True,
    on_worker_failure: Callable[[], None] = lambda: None,
) -> FastAPI:
    """Create the HTTP application with the supplied storage and Codex connections.

    Before accepting requests, the lifespan handler initializes storage and imports
    any saved JSON queue. A failure in either step prevents startup. Unless
    run_worker is False, it starts the worker and requests its shutdown on exit.
    Shutdown waits up to three seconds for the worker to finish.
    """
    worker = DispatchWorker(store, connections, interval, on_worker_failure) if run_worker else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        maintenance.initialize()
        if legacy_path is not None:
            LegacyImporter(store).import_file(legacy_path)
        if worker:
            loop = asyncio.get_running_loop()

            def request_restart() -> None:
                loop.call_soon_threadsafe(on_worker_failure)

            worker.on_failure = request_restart
            worker.thread.start()
        try:
            yield
        finally:
            if worker:
                await asyncio.to_thread(worker.stop)

    service = QueueService(store, connections)
    app = create_app(
        service,
        port=port,
        version=version or package_version("carrie-on"),
        build_id=build_id,
        worker=worker,
        lifespan=lifespan,
    )
    app.state.service = service
    return app


def main() -> int:
    """Read command-line settings and start the local server."""
    from carrie_on.macos.installation import Settings, SettingsOverrides
    from carrie_on.macos.runtime import Runtime

    runtime = Runtime.current()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=runtime.state)
    parser.add_argument("--codex")
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--interval", type=int)
    parser.add_argument("command", choices=["serve"])
    args = parser.parse_args()
    try:
        runtime = replace(runtime, data=args.state.absolute().parent)
        settings = Settings.load(
            runtime,
            SettingsOverrides(
                codex=args.codex, port=args.port, interval=args.interval, socket=args.socket
            ),
        )
        connections = CodexConnections(
            settings.codex, Path(settings.socket) if settings.socket else None, settings.codex_home
        )
        build_id = None
        if runtime.bundled:
            build_id = json.loads((runtime.app / "Contents/Resources/release.json").read_text())[
                "build_id"
            ]
        app = create_application(
            SqliteQueueStorage(args.state),
            connections,
            maintenance=SqliteStorageMaintenance(args.state),
            legacy_path=args.state.with_name("resume-queue.json"),
            port=settings.port,
            version=runtime.version,
            build_id=build_id,
            interval=settings.interval,
            on_worker_failure=lambda: setattr(server, "should_exit", True),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=settings.port,
                workers=1,
                access_log=False,
                timeout_graceful_shutdown=3,
            )
        )
        server.run()
        return 1 if app.state.worker and app.state.worker.error else 0
    except (OSError, ValueError, StorageUnavailable) as error:
        print(f"Carrie On server: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
