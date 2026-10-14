# Carrie On

Resume selected local Codex chats after their usage quota resets, from your Mac's menu bar.

Carrie On keeps a queue of chats and continuation instructions. It checks quota metadata without model inference and asks the running Codex desktop app to continue each eligible chat once. The resumed work uses the chat's existing model, tools, and permissions.

## Install

Download the DMG from [Releases](https://github.com/nickcorin/carrie-on/releases), open it, and drag **Carrie On** onto **Applications**. Eject the disk image and open the installed app, then use the menu bar to find a chat and add it to the queue. A ZIP and checksums are also available.

The bundle includes Python. You need an Apple Silicon Mac, the Codex desktop app, and a signed-in Codex CLI. Keep Codex open and your Mac awake and connected. The build targets macOS 13+, but runtime testing on macOS 13 is still outstanding. Releases are ad-hoc signed and are not notarized; macOS may require approval through Privacy & Security.

See the [installation guide](INSTALL.md) for first launch, updates, troubleshooting, and removal.

Installed standalone apps offer **Settings → Check for Updates…** in the menu bar, with verified downloads and **Install and Restart**. Updates preserve your queue and settings.

## How it works

Each enrollment authorizes a single continuation after a recorded reset. Carrie On waits for fresh quota headroom and checks that the chat still matches the enrolled work. Active chats and chats awaiting approval or input are deferred. Changed work, paused goals, and uncertain delivery require attention rather than automatic retries. You can pause the queue or remove an enrollment at any time.

Queue data, instructions, and logs stay on your Mac. The native card uses a local API listening only on `127.0.0.1`. Carrie On does not purchase credits, change models, or run a competing agent process. Remote chats and ChatGPT conversations are unsupported.

The desktop integration uses a private, version-sensitive IPC protocol. Unsupported snapshots stop dispatch. After a Codex update, check connectivity before relying on unattended continuation. This is an independent project, not an official OpenAI product.

## Documentation

- [Usage guide](USAGE.md): native card, terminal commands, and queue behavior.
- [Installation](INSTALL.md): install, update, recover, and uninstall.
- [Contributing](CONTRIBUTING.md): development setup, component map, and tests.
- [Releasing](RELEASING.md): build locally and prepare GitHub releases.
- [Feature contracts](features/index.md), [changelog](CHANGELOG.md), and [security](SECURITY.md).

Report reproducible bugs through [GitHub Issues](https://github.com/nickcorin/carrie-on/issues). Include app, macOS, and Codex versions; redact chat contents, account identifiers, and local paths from diagnostics.

Command filenames, installed service identities, and the Application Support directory use the Carrie On identity. See [installation and upgrades](INSTALL.md) before upgrading an installation with an older identity.

## License

[MIT](LICENSE), copyright Nicholas Corin. Bundled third-party components retain their own licenses, included inside the app.
