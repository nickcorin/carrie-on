# Distribution

## Sub-features

- Ship a single ad-hoc signed Carrie On.app containing Python, the local API, worker, native menu, and custom app icon.
- Configure backend and menu login services on first launch without development tools.
- Detect app replacements and update service registrations while preserving connection settings, queue, and history.
- Automatically migrate older bundled installations on first launch, merging queued work and retiring old services after the current installation is healthy.
- Check for a newer stable GitHub release on startup and show **Update to &lt;version&gt;** when available. Download and install it after confirmation.
- Rebuild the entire Carrie On Dev app from its saved checkout with live output, replacing and restarting only after tests, signing, and bundle verification succeed. Source menu installations reuse their native menu builder.
- Build versioned releases with automated acceptance checks, checksums, and release manifests.
- Offer a DMG with an illustrated drag-to-Applications window and the same signed app as the updater ZIP.
- Remove login services while preserving personal data.
- Build Carrie On Dev with isolated settings, queue, login services, logs, and port using the same runtime behavior as release.

## Setup and usage

Follow the [installation guide](../INSTALL.md) for setup, updates, release commands, and recovery. The app must reside directly in `/Applications` or `~/Applications`. Its Apple Silicon build targets macOS 13+, but runtime testing on macOS 13 is still outstanding. Codex desktop and a signed-in Codex CLI remain prerequisites.

First launch finds or asks for the CLI executable, saves settings in Application Support, registers services, and checks the local API before opening the menu. It never modifies the signed app. Fresh installations contain no enrolled chats, logs, or builder credentials. Replacing and reopening the app updates services based on its build identity. Reopening the same build preserves healthy running services.

## Legacy migration

Version 0.5.6 imports data from older bundled installations on first launch. Versions 0.5.3 and 0.5.4 reject updates with a different bundle identity. They require manual app replacement before automatic migration can run. Earlier versions have no built-in updater. Version 0.5.5 can update through Settings.

Migration stops the old services before importing settings, queued work, history, and the launch-at-login preference. When both installations have data, current settings and login preferences take precedence. Distinct queued chats and history are combined. Completed enrollments are never reactivated.

For a chat enrolled in both legacy JSON queues, an attempted or uncertain send takes precedence over waiting work. Otherwise, the current enrollment wins. Superseded entries remain in history and recovery copies. SQLite work takes precedence over later legacy imports; conflicting old items remain cancelled history. Old pause settings are ignored. In-flight attempts become expired deliveries, and held entries become changed items. Migration does not send messages.

After the new services are healthy, setup removes the old login registrations and moves the old data into `~/Library/Application Support/Carrie On/Migration/previous-installation/retired`. The parent directory holds snapshots of both installations and `journal.json`, which records migration progress. Unsupported or damaged files remain available for recovery, with warnings in the journal. Migration imports usable data automatically.

Reopening retries interrupted setup without importing stale queue entries over newer work. The installer snapshots existing SQLite using its backup API, including committed WAL data. Carrie On Dev never migrates the release installation. Direct source servers default to checkout-local SQLite; registered source services use the selected profile’s Application Support data. Registering the release source service stops the old writers and imports a private snapshot of checkout-local JSON once. Dev source registration does not import the release queue. See the [source upgrade instructions](../INSTALL.md#update-a-source-installation).

## Update verification and recovery

The updater checks the download's size, SHA-256 checksum, compatibility, bundle identity, and code signature before replacement. It trusts this project's GitHub release assets over HTTPS. Checksums detect damaged downloads. Ad-hoc signatures validate bundle integrity but do not authenticate the developer. The updater does not change Gatekeeper settings or request administrator privileges.

Installation exchanges complete bundles atomically and keeps the previous app in a hidden `.Carrie-On-update-*` directory beside the installed app. The new app restarts its worker and local API. It preserves the CLI path, port, interval, socket, Codex home, queue, history, and launch-at-login preference. See [update recovery](../INSTALL.md#recover-a-failed-update) for rollback instructions.

## Verification

Dev compares its built commit with the local checkout's `HEAD` on startup. It offers **Update to &lt;short hash&gt;** only when the built commit is an ancestor of `HEAD`. Otherwise it shows **Check for Updates**. `python -m unittest tests.test_source_build tests.test_app_update -v` tests both labels and checkouts with newer, equal, older, unrelated, or missing commits.

`just dev` builds and verifies the development profile with the same acceptance
pipeline as release. `python -m unittest tests.test_build_profiles -v` exercises both
profiles in the same isolated home, including independent queue state, worker
definitions, ports, and startup preferences. See the
[development build instructions](../RELEASING.md#local-development-app).

For source builds, see [menu usage](../USAGE.md#menu-bar). `python -m unittest tests.test_source_build -v` checks streamed build diagnostics, failed builds, staging with saved build paths and a custom port, signing, and replacement after success. The checks use temporary apps without stopping or installing live services.

`python -m tools.build_bundle` runs unit tests, native self-tests, linkage audits, code-signature checks, and `tools.verify_bundle` before writing packages to `dist/`. The verifier exercises the native setup helper in an isolated home without loading login services. Tests cover interrupted setup, updates, stable reopening, settings and queue preservation, legacy connection migration, and invalid install locations. See the [release verification guide](../INSTALL.md#release-verification) for tests on another Mac.

`python -m unittest tests.test_app_update -v` tests startup checks, offline failures, release selection, and download verification. It also replaces temporary apps to test native installation, rollback after a failed launch, and retry. It never replaces the installed app. See [updates](../INSTALL.md#updates) for recovery and download verification details.

`python -m unittest tests.test_service_installation tests.test_cli -v` checks source commands from outside the checkout, virtual-environment interpreter preservation, saved-setting validation, relative socket reuse, environment isolation, installation locking, publication failure recovery, source JSON import and retry, and interrupted CLI creation with its original UUID.

`python -m unittest tests.test_legacy_migration -v` exercises first launch with old settings, queues, history, startup preferences, and service registrations in isolated homes. It checks duplicate enrollments, uncertain sends, damaged data, interrupted import, setup failures, retries, and Dev isolation with a simulated launchd session. `carrie_on.macos.legacy_migration` manages recovery copies and the import journal.

## Distribution limits

The builder mounts the DMG read-only, checks the Applications shortcut, and copies its app into an isolated directory. It verifies that the copied app's contents and code signature match the ZIP's app. The release manifest records the DMG's filename, size, and SHA-256 hash. Release checks require both package formats and their checksum files. Built-in updates use the ZIP.

Ad-hoc signing does not provide notarization or remove Gatekeeper approval. Binary deployment targets cannot substitute for runtime tests on the oldest OS.

Bundled setup shares release service labels and Application Support data with a registered release source backend. A direct source SQLite database is not merged automatically. A setup failure can leave services stopped until a retry succeeds. Both CLI service registration and bundled setup hold the same per-profile lock while reading settings, validating configuration, and publishing service definitions.

**Quit Carrie On** stops background jobs and the menu. Reopening restarts them and preserves the launch-at-login preference. Uninstalling requires service removal before deleting the app.
