# Install Carrie On on another Mac

You need an Apple Silicon Mac, the Codex desktop app, and a signed-in Codex CLI. Carrie On includes Python and needs no development tools. Keep Codex open and your Mac awake and connected for automatic continuation.

The build targets macOS 13 or later. The current release has been tested on macOS 27.0.1. Runtime testing on macOS 13 remains outstanding.

## Install

To install the app, follow these steps:

1. Download and open the DMG from [GitHub Releases](https://github.com/nickcorin/carrie-on/releases).
2. If an older Carrie On is running, choose **Quit Carrie On**.
3. Drag **Carrie On** onto the **Applications** shortcut in the installer window. If you cannot write to `/Applications`, use `~/Applications` instead. The app must be directly inside either folder.
4. Eject the disk image and open the installed **Carrie On** app.
5. If setup cannot find Codex, choose its executable in the dialog. Run `command -v codex` in Terminal to find its path.
6. Open the menu bar item and queue a local chat.

If you prefer the ZIP, extract it and drag the app into Applications, then continue from step 4 without ejecting a disk image.

The app is ad-hoc signed and is not notarized. If macOS blocks a downloaded copy, open **System Settings**, select **Privacy & Security**, and choose **Open Anyway** for Carrie On. Open the app again. Only approve a release you trust. Managed Macs may prohibit this approval.

Setup runs as your login user, without administrator privileges. Your settings, queue, history, and logs are stored in `~/Library/Application Support/Carrie On`.

## Updates

These instructions apply to Carrie On releases. Carrie On Dev rebuilds from your local checkout. See [development builds](RELEASING.md#local-development-app).

### Update 0.5.5 or later

Carrie On checks for a newer stable release on startup. To install an update, follow these steps:

1. Open **Settings** from the menu bar item.
2. Choose **Update to &lt;version&gt;**. If that option is absent, choose **Check for Updates**.
3. Choose **Install and Restart**.

Settings shows download and installation progress. Choose **Not Now** to leave the update for later, or **Release Notes** to open its GitHub page. You do not need a GitHub account.

Updates preserve your settings, queue, history, and launch-at-login preference. See [startup and quit](features/app-lifecycle.md) for background service behavior.

### Replace 0.5.4 or earlier

The older updater rejects the changed app identity. To upgrade, follow the [installation steps](#install) to quit the old app and replace it with the latest release.

First launch imports your settings, queued work, history, and launch-at-login preference automatically. You do not need to move files, run Terminal commands, or resolve duplicate records. After the new services are healthy, setup removes the old service registrations and moves the old files into a recovery directory. See [migration behavior](features/distribution.md#legacy-migration) for merge rules and recovery files.

If migration is interrupted, reopen the app to resume it. To recover old files, look in `~/Library/Application Support/Carrie On/Migration/previous-installation`. Keep that directory until you have checked your settings, queue, and history.

### Recover a failed update

If a download or verification fails, resolve the reported problem and retry from **Check for Updates**. These failures leave the installed app unchanged.

If the replacement fails to start immediately, the updater restores the previous app. If startup fails later, reopen Carrie On to retry setup. To restore the previous app yourself, quit Carrie On and copy it from the hidden `.Carrie-On-update-*` directory beside the installed app or its release ZIP. Press **Command-Shift-.** in Finder to show hidden folders. Remove old app recovery directories after the update works. Back up your Application Support folder before downgrading across a documented data-format migration.

### Update a source installation

Update your Git checkout, run `just setup`, and register its backend with `.venv/bin/carrie-on service install`. Registration uses `~/Library/Application Support/Carrie On`, including `queue.sqlite3`, `settings.json`, and `.runtime/` logs. It shares the release app’s service names and data, so choose either the source service or the release app as the owner of that installation. Prefer `just dev` for an independent development app.

When registering a release source backend, the installer stops the retired worker and backend, takes a private copy of the checkout’s `resume-queue.json` in `Migration/source-checkout/resume-queue.json` under Application Support, and imports it transactionally. The original file and snapshot remain intact. Repeating registration does not reactivate completed work. Existing SQLite enrollments take precedence over an old JSON enrollment for the same chat. `--profile dev service install` uses the Dev namespace and does not adopt the release checkout queue.

A directly launched `carrie-on-server serve` instead defaults to `queue.sqlite3` beside the checkout, with adjacent `settings.json` when present. It imports an adjacent legacy JSON queue on first start. Its output goes to the terminal. `--state /absolute/path/queue.sqlite3` selects another database and settings directory. These direct servers are separate from registered services; use a different port when running both.

To switch from a registered source backend to the bundled release app, stop the source service and open the app; both use the same Application Support data. If you have only used a direct source server, stop it first and register the release source backend to migrate its legacy JSON. A direct source SQLite database is not merged automatically: finish or cancel its queued work before switching, or back up and move its entire data directory while all affected processes are stopped. Never overwrite an existing installation’s database to combine queues.

## Configuration and recovery

The bundled backend is `Carrie On.app/Contents/MacOS/carrie_on`. These examples assume the app is in `~/Applications`. Substitute `/Applications` if installed there.

```sh
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" setup --port 8877 --interval 90
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" setup --codex /absolute/path/to/codex
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" setup --socket /absolute/path/to/ipc.sock --codex-home /absolute/path/to/codex-home
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" doctor
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" queue list --history
```

Setup options update only the specified settings. Reopen the menu after changing its port. To clear a custom socket or Codex home, set its value to `null` in `settings.json` and reopen. Setup reads `CODEX_HOME` from the environment only when no choice is saved. Later launches preserve the saved choice, including `null` for the default.

Run `doctor` to check sign-in and desktop connectivity without starting a model turn. Setup checks that the CLI executes but does not check those connections.

If setup is interrupted, reopen the app or choose **Retry** in the error dialog to repair the worker and local API. A failure can leave services stopped until setup succeeds. For an occupied port, run setup with a free port and reopen.

Logs are in `~/Library/Application Support/Carrie On/.runtime/`, including `server.log` and `server-error.log`. The login jobs are `com.nickcorin.carrieon.ui` (backend and dispatch worker) and `com.nickcorin.carrieon.menu`. Setup retires the former standalone worker. macOS Background Items settings must allow these services. Advanced repair is available through `setup --repair`.

Queue state and delivery history live in `queue.sqlite3`. First start imports the adjacent `resume-queue.json` once and retains it as a recovery copy. Pause is removed. Expired delivery leases recover interrupted sends through fresh Codex checks. Back up the whole data directory with the app stopped, or use SQLite's backup API while it runs; copying only an open database file can omit its WAL. Old releases cannot read new SQLite state. See [migration details](ARCHITECTURE.md#installation-and-migration).

To uninstall, remove the login services before deleting the app:

```sh
"$HOME/Applications/Carrie On.app/Contents/MacOS/carrie_on" remove-installation --yes
```

Then quit Carrie On and move it to Trash. Your settings, queue, history, and logs remain in Application Support. Delete that directory only if you also want to discard personal data. The CLI's `service uninstall` removes only the backend registration.

## Build and release

See [Releasing](RELEASING.md) for the pinned build environment, local builds, checksums, GitHub CI, and draft releases.

## Release verification

To rerun the packaged acceptance check:

```sh
just verify
```

It extracts into paths containing spaces, copies the app to an isolated home, removes Python from PATH, and invokes the native first-launch helper with `--no-start`. It checks service paths, persisted configuration, queue operations, local API health, release health, app immutability, and code signatures. Its fake Codex executable only answers the setup version check. It never loads login services or dispatches a chat. `--no-start` is restricted to fresh test installations.

Before broader distribution, test Gatekeeper approval, first launch, login startup, `doctor`, updates, and removal on another Mac. Include macOS 13 before claiming runtime compatibility with that version. Local tests on macOS 27.0.1 and deployment-target checks do not establish that compatibility. See [automated distribution checks](features/distribution.md#verification) for unit test coverage.

## Command reference

`carrie_on setup [--codex PATH] [--socket PATH] [--codex-home PATH] [--port PORT] [--interval SECONDS] [--repair] [--no-start] [--json]` registers per-user services and saves settings. The default port is 8766, with allowed values from 1024 to 65535. The default interval is 60 seconds, with a minimum of 30. The bundled queue location is fixed. An isolated server accepts `--state PATH`; clients select it with `--server URL`.

`carrie_on remove-installation --yes` stops and removes both current login services and any retired worker registration, retaining the app and personal data. Setup commands return 0 on success, 1 for environment errors, 2 for argument errors, and 130 for interruption. `--json` returns structured setup success or failure on stdout. Ordinary failures use stderr. CLI `--version` and the app's Info.plist report `VERSION`.
