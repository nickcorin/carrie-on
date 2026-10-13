# Distribution

## Sub-features

- Ship a single ad-hoc signed Carrie On.app containing Python, the dashboard, worker, native menu, and custom app icon.
- Configure three per-user login services on first launch without development tools.
- Detect app replacements and reconcile services while preserving connection settings, queue, and history.
- Check published stable GitHub releases, verify a compatible download, and install it from the menu with explicit consent.
- Build versioned releases with automated acceptance checks, checksums, and release manifests.
- Offer a DMG with an illustrated drag-to-Applications window and the same signed app as the updater ZIP.
- Remove login services while preserving personal data.

## Setup and usage

Follow the [installation guide](../INSTALL.md) for setup, updates, release commands, and recovery. The app must reside directly in `/Applications` or `~/Applications`. Its Apple Silicon build targets macOS 13+, but runtime testing on macOS 13 is still outstanding. Codex desktop and a signed-in Codex CLI remain prerequisites.

First launch discovers or asks for the CLI executable, saves settings in Application Support, registers services, and checks dashboard readiness before opening the menu. It never modifies the signed app. Fresh installations contain no enrolled chats or builder credentials. Replacing and reopening the app refreshes services based on its build identity; ordinary reopening preserves running services.

## Verification

`build_bundle.py` runs unit tests, native self-tests, linkage audits, code-signature checks, and `verify_bundle.py` before publishing artifacts to `dist/`. The verifier exercises the native setup helper in an isolated home without loading login services. Tests cover interrupted setup, updates, stable reopening, settings and queue preservation, legacy connection migration, and invalid install locations. See the installation guide for real-Mac release acceptance checks.

`python3 -m unittest test_app_update -v` exercises numeric version ordering, stable release eligibility, compatibility, exact-version download selection, corrupted and hostile archives, signed-bundle staging, and the real native atomic swap with launch-failure rollback and retry. These tests use temporary apps and never replace the installed app. Network checks contact GitHub only when the user chooses Check for Updates; installation requires a writable Applications folder. Recovery copies remain in hidden sibling directories. See [updates](../INSTALL.md#updates) for recovery and trust details.

## Gotchas

The builder mounts the DMG read-only, checks the Applications shortcut, copies its app into an isolated directory, and verifies that its contents and code signature match the ZIP's app. The release manifest records the DMG's own filename, size, and SHA-256 hash; release checks require both package formats and their checksum files. Eject the DMG after dragging the app into Applications, and open the installed copy. Built-in updates continue to use the ZIP.

Ad-hoc signing does not provide notarization or remove Gatekeeper approval. Binary deployment targets cannot substitute for runtime tests on the oldest OS. Setup replaces existing source service labels without importing project-local queues. Service registration is recoverable but not transactional; retry setup after correcting failures. Quit Carrie On stops background jobs and the menu; reopening restarts them and preserves the launch-at-login preference. Removal must stop services before the app is discarded. The existing Tokenmaxxer service identifiers and data directory remain stable across the rename; see the [upgrade instructions](../INSTALL.md#updates).
