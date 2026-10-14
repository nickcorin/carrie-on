# Distribution

## Sub-features

- Ship a single ad-hoc signed Carrie On.app containing Python, the local API, worker, native menu, and custom app icon.
- Configure three per-user login services on first launch without development tools.
- Detect app replacements and reconcile services while preserving connection settings, queue, and history.
- Automatically migrate older bundled installations on first launch, merging queued work and retiring old services after the current installation is healthy.
- Check for a newer stable GitHub release on startup and show **Update to <version>** when available. Download and install it after confirmation.
- Rebuild the entire Carrie On Dev app from its saved checkout with live output, replacing and restarting only after tests, signing, and bundle verification succeed. Source menu installations reuse their native menu builder.
- Build versioned releases with automated acceptance checks, checksums, and release manifests.
- Offer a DMG with an illustrated drag-to-Applications window and the same signed app as the updater ZIP.
- Remove login services while preserving personal data.
- Build Carrie On Dev with isolated settings, queue, login services, logs, and port using the same runtime behavior as release.

## Setup and usage

Follow the [installation guide](../INSTALL.md) for setup, updates, release commands, and recovery. The app must reside directly in `/Applications` or `~/Applications`. Its Apple Silicon build targets macOS 13+, but runtime testing on macOS 13 is still outstanding. Codex desktop and a signed-in Codex CLI remain prerequisites.

First launch discovers or asks for the CLI executable, saves settings in Application Support, registers services, and checks local API readiness before opening the menu. It never modifies the signed app. Fresh installations contain no enrolled chats or builder credentials. Replacing and reopening the app refreshes services based on its build identity; ordinary reopening preserves running services.

## Verification

Dev compares its built commit with the local checkout's `HEAD` on startup. It offers **Update to <short hash>** only when the built commit is an ancestor of `HEAD`. Otherwise it shows **Check for Updates**. `python3 -m unittest test_source_build test_app_update -v` tests both labels and checkouts with newer, equal, older, unrelated, or missing commits.

`just dev` builds and verifies the development profile with the same acceptance
pipeline as release. `python3 -m unittest test_build_profiles -v` exercises both
profiles in the same isolated home, including independent queue state, worker
definitions, ports, and startup preferences. See the
[development build instructions](../RELEASING.md#local-development-app).

For source builds, see [menu usage](../USAGE.md#menu-bar). `python3 -m unittest test_source_build -v` checks streamed build diagnostics, failed builds, staging with saved build paths and a custom port, signing, and replacement after success. The checks use temporary apps without stopping or installing live services.

`build_bundle.py` runs unit tests, native self-tests, linkage audits, code-signature checks, and `verify_bundle.py` before publishing artifacts to `dist/`. The verifier exercises the native setup helper in an isolated home without loading login services. Tests cover interrupted setup, updates, stable reopening, settings and queue preservation, legacy connection migration, and invalid install locations. See the installation guide for real-Mac release acceptance checks.

`python3 -m unittest test_app_update -v` tests startup checks, offline failures, release selection, and download verification. It also replaces temporary apps to test native installation, rollback after a failed launch, and retry. It never replaces the installed app. See [updates](../INSTALL.md#updates) for recovery and download verification details.

`python3 -m unittest test_legacy_migration -v` exercises first launch with old settings, queue/history, startup preferences and service registrations in isolated homes. It checks duplicate enrollments, uncertain sends, damaged data, interrupted import, setup failures, retries, and Dev isolation with a simulated launchd session. Recovery copies and the import journal belong to `legacy_migration.py`; the current bundle identity and updater remain unchanged.

## Gotchas

The builder mounts the DMG read-only, checks the Applications shortcut, copies its app into an isolated directory, and verifies that its contents and code signature match the ZIP's app. The release manifest records the DMG's own filename, size, and SHA-256 hash; release checks require both package formats and their checksum files. Eject the DMG after dragging the app into Applications, and open the installed copy. Built-in updates continue to use the ZIP.

Ad-hoc signing does not provide notarization or remove Gatekeeper approval. Binary deployment targets cannot substitute for runtime tests on the oldest OS. Setup replaces existing source service labels without importing project-local queues. Service registration is recoverable but not transactional; retry setup after correcting failures. Quit Carrie On stops background jobs and the menu; reopening restarts them and preserves the launch-at-login preference. Removal must stop services before the app is discarded. Installations with an older identity require manual app replacement, followed by automatic data and service migration; see the [upgrade instructions](../INSTALL.md#updates).
