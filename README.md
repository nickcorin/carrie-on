# Carrie On

Resume selected local Codex chats after their usage quota resets, from your Mac's menu bar.

Carrie On keeps a queue of chats and continuation instructions. It checks quota metadata without model inference and asks the running Codex desktop app to continue each eligible chat once. The resumed work uses the chat's existing model, tools, and permissions.

## Install

Download the DMG from [Releases](https://github.com/nickcorin/carrie-on/releases), open it, and drag **Carrie On** onto **Applications**. Eject the disk image and open the installed app, then use the menu bar to find a chat and add it to the queue. A ZIP and checksums are also available.

The bundle includes Python. You need an Apple Silicon Mac, the Codex desktop app, and a signed-in Codex CLI. Keep Codex open and your Mac awake and connected. The build targets macOS 13 or later, but runtime testing on macOS 13 remains outstanding. Releases are ad-hoc signed and are not notarized. macOS may require approval through **Privacy & Security**.

See the [installation guide](INSTALL.md) for first launch, updates, troubleshooting, and removal.

Carrie On checks for updates on startup. Open **Settings** and choose **Update to &lt;version&gt;** to install an available release, or **Check for Updates** to check again. Updates preserve your queue and settings.

If you use 0.5.4 or earlier, quit the old app and replace it with the downloaded app. Version 0.5.6 imports your data and removes the old service registrations automatically. See the [update instructions](INSTALL.md#updates).

Carrie On Dev checks for newer commits in your local checkout and rebuilds the app when you choose **Update to &lt;short hash&gt;**. See [development builds](RELEASING.md#local-development-app).

## How it works

Each enrollment authorizes a single continuation after a recorded reset. Carrie On waits for available quota and checks that the chat still matches the enrolled work. It waits while chats are active or need approval or input. Changed work, paused goals, and uncertain delivery require attention rather than automatic retries. You can pause the queue or remove an enrollment at any time.

Queue data, instructions, and logs stay on your Mac. The native card uses a local API listening only on `127.0.0.1`. Carrie On does not purchase credits, change models, or run a competing agent process. Remote chats and ChatGPT conversations are unsupported.

New enrollments use the official Python SDK to read quota, chat metadata, and the latest turn header. Legacy queue migration and delivery recovery also read opening prompts, as described in the [continuation contract](features/continuation-queue.md).

Dispatch uses the desktop's private, version-sensitive IPC protocol. Missing completion evidence keeps a chat waiting. Incompatible responses prevent dispatch for the affected chat. After a Codex update, check connectivity before relying on unattended continuation. This is an independent project, not an official OpenAI product.

## Documentation

- [Usage guide](USAGE.md): native card, terminal commands, and queue behavior.
- [Installation](INSTALL.md): install, update, recover, and uninstall.
- [Contributing](CONTRIBUTING.md): development setup, component map, and tests.
- [Releasing](RELEASING.md): build locally and prepare GitHub releases.
- [Feature contracts](features/index.md), [changelog](CHANGELOG.md), and [security](SECURITY.md).

Report reproducible bugs through [GitHub Issues](https://github.com/nickcorin/carrie-on/issues). Include the app, macOS, and Codex versions. Redact chat contents, account identifiers, and local paths from diagnostics.

## License

[MIT](LICENSE), copyright Nicholas Corin. Bundled third-party components retain their own licenses, included inside the app.
