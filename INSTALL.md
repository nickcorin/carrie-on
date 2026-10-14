# Install Carrie On on another Mac

Carrie On ships as a single **Carrie On.app** containing its native menu interface, Python, worker, and local API. The destination needs no Python, Xcode, or runtime download. It does need the Codex desktop app and a signed-in Codex CLI. Keep Codex open and the Mac awake and connected for automatic continuation.

## Install

1. Download and open the release DMG. The `arm64` release targets **Apple Silicon and macOS 13 or later**. Runtime testing on macOS 13 is still outstanding; the current release has been exercised on macOS 27.0.1.
2. Drag **Carrie On** onto the **Applications** shortcut in the installer window, then eject the disk image. If you cannot write to `/Applications`, drag the app into your home folder's `Applications` folder instead. It must be directly inside either folder. Quit an older Carrie On before replacing it. The ZIP remains available as an alternative: extract it and drag the app into Applications.
3. Open **Carrie On**. First launch checks for the Codex CLI, saves its location, and sets up the worker, local API, and menu login services. If Codex cannot be found, choose its executable in the setup dialog. `command -v codex` in Terminal shows the path on a configured Mac.
4. Open the menu bar item and queue a local chat.

The app is **ad-hoc signed, not notarized**. A downloaded copy may be blocked. After attempting to open it, approve that specific app in **System Settings → Privacy & Security → Open Anyway**, then open it again. Only approve a release you trust. No Apple developer membership is needed for this distribution, but it cannot offer the warning-free first launch of a Developer ID signed and notarized app. Managed Macs may prohibit this approval.

Setup runs as your login user, without administrator privileges. Settings, queue/history, logs, and setup records live in `~/Library/Application Support/Carrie On`. Nothing is written into the signed app. No builder credentials, chats, queue entries, or logs enter the release archive.

## Updates

**Carrie On Dev** shows **Update to <short hash>** when behind its local checkout, or **Check for Updates** when current. Updating rebuilds its entire local app. See [development builds](RELEASING.md#local-development-app) for progress, failure handling, and initial installation. The release-update flow below applies to **Carrie On**.

Carrie On quietly checks the latest published stable GitHub release on startup. When an update is available, the settings button reads **Update to <version>**. Open the menu bar popover, choose that action, then **Install and Restart**. Otherwise, choose **Settings → Check for Updates** to check manually. A failed startup check leaves manual checking available without interrupting you.

Carrie On downloads the version you accepted and verifies its size, SHA-256 checksum, compatibility, bundle identity, and code signature before replacing the app. The settings action reports checking, downloading, and installation progress. No GitHub account or token is needed. **Not Now** leaves the update available for later, and **Release Notes** opens that version's GitHub page. Downloads and installation require your confirmation.

The app restarts its worker and local API from the replacement bundle. It preserves the saved CLI path, port, interval, socket, Codex home, queue/history, and launch-at-login preference. Reopening the same build leaves healthy services running. Quit stops the app and its background services; reopening restarts them. The settings menu's **Launch at Login** setting controls automatic startup independently of manual launches. See [startup and quit](features/app-lifecycle.md).

The first release containing the updater must be installed manually. For older versions, source installations, or an Applications folder you cannot write to, choose **Quit Carrie On**, replace **Carrie On.app** with the downloaded release, and reopen it. Source installations should update their Git checkout and rebuild instead. macOS may request approval for a downloaded build.

When upgrading an installation with an older app identity, use the old app's backend to run `remove-installation --yes` before quitting it. For source installations, uninstall the old worker and remove the old API and menu services using that version's instructions. Back up the old Application Support directory, then copy `settings.json`, `resume-queue.json`, and any logs you want to retain into `~/Library/Application Support/Carrie On` before opening the new app. Do not copy `Lifecycle`, `installation.json`, or lock files: setup must create new service registrations. The new bundle identity requires a manual installation; the older updater cannot accept it. Remove the old app after the new installation works.

Network, compatibility, or verification failures leave the installed app unchanged. Retry from **Check for Updates…** after resolving the problem. Installation exchanges complete bundles atomically and keeps the previous app in a hidden `.Carrie-On-update-*` directory beside the installed app. If starting the replacement fails immediately, it restores the previous bundle. If startup fails later, reopen Carrie On for setup recovery, or quit it and restore the previous app from that directory or its release ZIP. Finder's **Command-Shift-.** shows hidden folders; remove old recovery directories once the update is working. Back up your Application Support folder before downgrading across a documented data-format migration.

Updates trust this project's GitHub release assets over HTTPS. Checksums detect damaged downloads; ad-hoc signatures validate bundle integrity but do not authenticate a developer identity. The updater does not change Gatekeeper settings or request administrator privileges.

For older bundled installations, follow the identity migration above before setup. Source installations keep a project-local queue; finish or cancel existing enrollments deliberately before switching, or copy the queue while all workers are stopped.

## Configuration and recovery

The bundled backend is `Carrie On.app/Contents/MacOS/carrie_on`. These examples assume the app is in your home Applications folder; substitute `/Applications` if installed there.

```sh
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" setup --port 8877 --interval 90
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" setup --codex /absolute/path/to/codex
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" setup --socket /absolute/path/to/ipc.sock --codex-home /absolute/path/to/codex-home
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" doctor
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" list --history
```

Setup options update only the specified settings; reopen the menu after changing its port. Settings are stored in `settings.json`. To clear a custom socket or Codex home, set its value to `null` there and reopen. `CODEX_HOME` is captured from the environment only when the settings do not yet contain a Codex home choice. Later launches preserve the saved choice, including `null` for the default, regardless of their environment. `doctor` checks live connectivity without starting a model turn; setup checks that the CLI executes but does not validate sign-in or desktop connectivity.

If setup is interrupted, reopen the app to retry. Service registration is not transactional; a failure can leave services stopped until the next successful setup. Setup is protected by a per-user lock. **Retry** in the error dialog repairs the worker and local API. For an occupied port, run setup with a free port and reopen.

Logs are in `~/Library/Application Support/Carrie On/.runtime/`. The login jobs are `com.nickcorin.carrieon`, `com.nickcorin.carrieon.ui`, and `com.nickcorin.carrieon.menu`. macOS Background Items settings must allow these services. Advanced repair is available through `setup --repair`.

To uninstall, remove the login services before deleting the app:

```sh
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" remove-installation --yes
```

Then quit Carrie On and move it to Trash. Settings, queue/history, and logs remain in Application Support; delete that directory manually only if you also want to discard personal data. The source CLI's `uninstall` command removes only the worker.

## Build and release

See [Releasing](RELEASING.md) for the pinned build environment, local builds, checksums, GitHub CI, and draft releases.

## Release verification

To rerun the packaged acceptance check:

```sh
.venv/bin/python verify_bundle.py dist/Carrie-On-0.5.1-macos-arm64.zip
```

It extracts into paths containing spaces, copies the app to an isolated home, removes Python from PATH, and invokes the native first-launch helper with `--no-start`. It checks service paths, persisted configuration, queue operations, local API health, release health, app immutability, and code signatures. Its fake Codex executable only answers the setup version check. It never loads login services or dispatches a chat. `--no-start` is restricted to fresh test installations.

Unit tests exercise repeated setup, upgrades, preservation of settings and queue/history, recovery after partial registration, and migration of legacy connection settings. Before broader distribution, manually test Gatekeeper approval, real first launch, login startup, `doctor`, update, and removal on another Mac, including macOS 13 if claiming that OS as supported. Local tests on macOS 27.0.1 and deployment-target audits do not establish that compatibility.

## Command reference

`carrie_on setup [--codex PATH] [--socket PATH] [--codex-home PATH] [--port PORT] [--interval SECONDS] [--repair] [--no-start] [--json]` reconciles per-user services and persists settings. First-install defaults are port 8766 (1024–65535) and interval 60 seconds (minimum 30). The bundled queue location is fixed; worker commands still accept `--state PATH` for inspecting other queues.

`carrie_on remove-installation --yes` stops and removes all three login services, retaining the app and personal data. Setup commands return 0 on success, 1 for environment errors, 2 for argument errors, and 130 for interruption. `--json` returns structured setup success or failure on stdout; ordinary failures use stderr. Worker `--version` and the app's Info.plist report `VERSION`.
