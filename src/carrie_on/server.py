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
from carrie_on.codex.adapter import CodexAdapter
from carrie_on.codex.client import CodexConnections
from carrie_on.configuration import Settings, SettingsOverrides
from carrie_on.dispatch import DispatchWorker
from carrie_on.ports import Codex, QueueStorage, StorageMaintenance, StorageUnavailable
from carrie_on.storage import SqliteQueueStorage, SqliteStorageMaintenance


def create_application(
    store: QueueStorage,
    codex: Codex,
    *,
    maintenance: StorageMaintenance,
    port: int = 8766,
    version: str | None = None,
    build_id: str | None = None,
    interval: float = 60,
    run_worker: bool = True,
    on_shutdown: Callable[[], None] = lambda: None,
) -> FastAPI:
    """Own storage startup and the dispatch worker for the HTTP service.

    The host's on_shutdown callback stops HTTP serving. Both explicit shutdown
    and a fatal worker error use it. Storage failure prevents startup.
    """
    worker = DispatchWorker(store, codex, interval) if run_worker else None

    def shutdown() -> None:
        if worker:
            worker.stopping.set()
            worker.wakeup.set()
        on_shutdown()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        maintenance.initialize()
        if worker:
            loop = asyncio.get_running_loop()

            def request_restart() -> None:
                loop.call_soon_threadsafe(shutdown)

            worker.on_failure = request_restart
            worker.thread.start()
        try:
            yield
        finally:
            if worker:
                await asyncio.to_thread(worker.stop)

    app = create_app(
        store,
        codex,
        port=port,
        version=version or package_version("carrie-on"),
        build_id=build_id,
        worker=worker,
        shutdown=shutdown,
        lifespan=lifespan,
    )
    return app


def main() -> int:
    """Read command-line settings and start the local server."""
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
            runtime.data,
            SettingsOverrides(
                codex=args.codex, port=args.port, interval=args.interval, socket=args.socket
            ),
            default_port=runtime.profile.port,
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
            CodexAdapter(connections),
            maintenance=SqliteStorageMaintenance(args.state),
            port=settings.port,
            version=runtime.version,
            build_id=build_id,
            interval=settings.interval,
            on_shutdown=lambda: setattr(server, "should_exit", True),
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
