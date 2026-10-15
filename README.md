# Carrie On

Resume selected local Codex chats after their usage quota resets, from your Mac's menu bar.

Carrie On keeps queued instructions in SQLite. Its FastAPI service serves the native menu and Typer CLI, checks quota metadata without inference, and asks the running Codex desktop app to continue eligible work. Resumed work uses the chat's existing model, tools, and permissions.

## Install

Download the DMG from [Releases](https://github.com/nickcorin/carrie-on/releases), open it, and drag **Carrie On** onto **Applications**. Eject the disk image and open the installed app, then use the menu bar to find a chat and add it to the queue. A ZIP and checksums are also available.

The bundle includes Python. You need an Apple Silicon Mac, the Codex desktop app, and a signed-in Codex CLI. Keep Codex open and your Mac awake and connected. The build targets macOS 13 or later, but runtime testing on macOS 13 remains outstanding. Releases are ad-hoc signed and are not notarized. macOS may require approval through **Privacy & Security**.

See the [installation guide](INSTALL.md) for first launch, updates, troubleshooting, and removal.

Carrie On checks for updates on startup. Open **Settings** and choose **Update to &lt;version&gt;** to install an available release, or **Check for Updates** to check again. Updates preserve your queue and settings.

Upgrades from v0.5.7 preserve the JSON queue in SQLite and retain the source file for recovery. See the [update instructions](INSTALL.md#updates).

Carrie On Dev checks for newer commits in your local checkout and rebuilds the app when you choose **Update to &lt;short hash&gt;**. See [development builds](RELEASING.md#local-development-app).

## How it works

Each queue item authorizes an instruction when a quota window changes and quota is available. Carrie On identifies windows by their reset timestamps and checks that the chat still matches the enrolled work. It does not need to observe exhaustion. Active chats and work awaiting quota, approval, input, or goal changes remain retryable failures. If the conversation has continued, its entry becomes stale and does not retry. Expiring delivery leases recover interrupted workers; fresh conversation checks suppress duplicate sends after uncertain replies. Remove queued work using its trash control, or quit the app to stop processing while preserving the queue.

Queue data, instructions, and logs stay on your Mac. The native card uses a local API listening only on `127.0.0.1`. Carrie On does not purchase credits, change models, or run a competing agent process. Remote chats and ChatGPT conversations are unsupported.

The official Python SDK reads quota, chat metadata, the latest turn header, and its opening message identity through bounded requests. Recovery checks older turn headers when needed, as described in the [queue contract](features/queue.md).

Dispatch uses the desktop's private, version-sensitive IPC protocol. Missing completion evidence keeps a chat waiting. Incompatible responses prevent dispatch for the affected chat. After a Codex update, check connectivity before relying on unattended continuation. This is an independent project, not an official OpenAI product.

## Documentation

- [Usage guide](USAGE.md): native card, terminal commands, and queue behavior.
- [Installation](INSTALL.md): install, update, recover, and uninstall.
- [Contributing](CONTRIBUTING.md): development setup, component map, and tests.
- [Architecture and API](ARCHITECTURE.md): package boundaries, SQLite, delivery, and HTTP contracts.
- [Interface references](CONTRIBUTING.md#interface-references): generated Python and native declarations, also bundled with the app.
- [Releasing](RELEASING.md): build locally and prepare GitHub releases.
- [Feature contracts](features/index.md), [changelog](CHANGELOG.md), and [security](SECURITY.md).

Report reproducible bugs through [GitHub Issues](https://github.com/nickcorin/carrie-on/issues). Include the app, macOS, and Codex versions. Redact chat contents, account identifiers, and local paths from diagnostics.

## License

[MIT](LICENSE), copyright Nicholas Corin. Bundled third-party components retain their own licenses, included inside the app.
