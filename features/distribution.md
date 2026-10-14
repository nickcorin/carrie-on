# Distribution

## Sub-features

- Ship a single ad-hoc signed Carrie On.app containing Python, the local API, worker, native menu, and custom app icon.
- Configure three per-user login services on first launch without development tools.
- Detect app replacements and update service registrations while preserving connection settings, queue, and history.
- Automatically migrate older bundled installations on first launch, merging queued work and retiring old services after the current installation is healthy.
- Check for a newer stable GitHub release on startup and show **Update to <version>** when available. Download and install it after confirmation.
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

For a chat enrolled in both queues, an attempted or uncertain send takes precedence over waiting work. Otherwise, the current enrollment wins. Superseded entries remain in history and recovery copies. Migration preserves existing pauses and delivery uncertainty. It does not send messages or ask the user to resolve duplicate records.

After the new services are healthy, setup removes the old login registrations and moves the old data into `~/Library/Application Support/Carrie On/Migration/previous-installation/retired`. The parent directory holds snapshots of both installations and `journal.json`, which records migration progress. Unsupported or damaged files remain available for recovery, with warnings in the journal. Migration imports usable data automatically.

Reopening retries interrupted setup without importing stale queue entries over newer work. Carrie On Dev never migrates the release installation. Source installations keep project-local queues and need separate migration. See the [upgrade instructions](../INSTALL.md#updates).

## Update verification and recovery

The updater checks the download's size, SHA-256 checksum, compatibility, bundle identity, and code signature before replacement. It trusts this project's GitHub release assets over HTTPS. Checksums detect damaged downloads. Ad-hoc signatures validate bundle integrity but do not authenticate the developer. The updater does not change Gatekeeper settings or request administrator privileges.

Installation exchanges complete bundles atomically and keeps the previous app in a hidden `.Carrie-On-update-*` directory beside the installed app. The new app restarts its worker and local API. It preserves the CLI path, port, interval, socket, Codex home, queue, history, and launch-at-login preference. See [update recovery](../INSTALL.md#recover-a-failed-update) for rollback instructions.

## Verification

Dev compares its built commit with the local checkout's `HEAD` on startup. It offers **Update to <short hash>** only when the built commit is an ancestor of `HEAD`. Otherwise it shows **Check for Updates**. `python3 -m unittest test_source_build test_app_update -v` tests both labels and checkouts with newer, equal, older, unrelated, or missing commits.

`just dev` builds and verifies the development profile with the same acceptance
pipeline as release. `python3 -m unittest test_build_profiles -v` exercises both
profiles in the same isolated home, including independent queue state, worker
definitions, ports, and startup preferences. See the
[development build instructions](../RELEASING.md#local-development-app).

For source builds, see [menu usage](../USAGE.md#menu-bar). `python3 -m unittest test_source_build -v` checks streamed build diagnostics, failed builds, staging with saved build paths and a custom port, signing, and replacement after success. The checks use temporary apps without stopping or installing live services.

`build_bundle.py` runs unit tests, native self-tests, linkage audits, code-signature checks, and `verify_bundle.py` before writing packages to `dist/`. The verifier exercises the native setup helper in an isolated home without loading login services. Tests cover interrupted setup, updates, stable reopening, settings and queue preservation, legacy connection migration, and invalid install locations. See the [release verification guide](../INSTALL.md#release-verification) for tests on another Mac.

`python3 -m unittest test_app_update -v` tests startup checks, offline failures, release selection, and download verification. It also replaces temporary apps to test native installation, rollback after a failed launch, and retry. It never replaces the installed app. See [updates](../INSTALL.md#updates) for recovery and download verification details.

`python3 -m unittest test_legacy_migration -v` exercises first launch with old settings, queues, history, startup preferences, and service registrations in isolated homes. It checks duplicate enrollments, uncertain sends, damaged data, interrupted import, setup failures, retries, and Dev isolation with a simulated launchd session. `legacy_migration.py` manages recovery copies and the import journal.

## Distribution limits

The builder mounts the DMG read-only, checks the Applications shortcut, and copies its app into an isolated directory. It verifies that the copied app's contents and code signature match the ZIP's app. The release manifest records the DMG's filename, size, and SHA-256 hash. Release checks require both package formats and their checksum files. Built-in updates use the ZIP.

Ad-hoc signing does not provide notarization or remove Gatekeeper approval. Binary deployment targets cannot substitute for runtime tests on the oldest OS.

Setup replaces existing source service labels without importing project-local queues. A setup failure can leave services stopped until a retry succeeds. A per-user lock prevents concurrent setup attempts.

**Quit Carrie On** stops background jobs and the menu. Reopening restarts them and preserves the launch-at-login preference. Uninstalling requires service removal before deleting the app.
