# Changelog

## 0.5.6

Carrie On now imports settings, queued work, history, and the launch-at-login preference from older bundled installations when you first open the new app.

If both installations have data, Carrie On combines their queues without replaying completed work or retrying uncertain sends. It removes the old service registrations after the new services are healthy and keeps the previous files for recovery. Interrupted migration resumes when you reopen the app.

### Upgrade

If you use **0.5.4 or earlier**, follow these steps:

1. Choose **Quit Carrie On** in the old app.
2. Download and open the 0.5.6 DMG from this release.
3. Drag **Carrie On** into Applications and replace the existing app.
4. Eject the disk image and open the installed app.

Your data and services migrate automatically. You do not need to move files or run Terminal commands. The older updater rejects the changed app identity, so it cannot install this release itself.

If you use **0.5.5**, choose **Check for Updates** or **Update to 0.5.6** in Settings, then **Install and Restart**.

See the [installation guide](https://github.com/nickcorin/carrie-on/blob/v0.5.6/INSTALL.md#updates) for recovery instructions and source installations.

### Verification

Tested on Apple Silicon with macOS 27.0.1. Three chats were queued through the published 0.5.3 app, the earliest release with an updater. Its update attempt reproduced the identity error. After manual replacement with a 0.5.6 test build, all three entries migrated and the worker continued each exactly once through a simulated Codex connection. Reopening preserved their completed history without importing them again.

These tests used synthetic chats and a simulated Codex connection. Runtime testing on macOS 13 and another Mac remains outstanding.

## 0.5.5

- Check for updates once on startup. Show Update to <version> when a release is available, or Check for Updates when current.
- Let development builds discover newer local checkout commits and rebuild the complete app from the update action.
- Wait for background services to finish shutting down before replacing the app, avoiding false shutdown failures.
- Show the installed version in the settings menu.

- Use the Carrie On identity throughout Python modules, executable names, bundle metadata, login services, local API headers, and Application Support paths, including isolated development builds.
- Reject retired branding in source inputs and packaged application payloads before a release is produced.
- Remove the browser dashboard and bundle only the native menu interface and its local API.
- When upgrading an app with an older identity, install manually. Remove its services and migrate saved settings and queue data as described in the installation guide before opening the new app.

## 0.5.4

- Add a disk image with a drag-to-Applications installer window, alongside the ZIP used by built-in updates.
- Verify that the DMG and ZIP contain the same signed app and publish checksums for both formats.

## 0.5.3

- Add menu-driven GitHub update checks, verified downloads, and Install and Restart with atomic app replacement and recovery copies.
- Simplify chat pagination feedback to subtle loading placeholders while preserving retry and search behavior.

## 0.5.2

- Update the app and dashboard icons with a cobalt and porcelain C-arrow design.
- Generate the app and monochrome tray icons from a shared vector silhouette, preserving the same shape across sizes.
- Verify regenerated artwork against the reference silhouette to prevent shape drift.

## 0.5.1

- Add a justfile for local CI, building, signing, packaging, and draft releases without hosted CI credits.
- Keep GitHub CI manual-only and verify release artifacts against the current clean source commit.
- Add pagination feedback and regression coverage for browsing large chat catalogs.
- Validate release tags against the version and changelog before building.
- Include the MIT license and complete user documentation in release bundles.
- Report the current app version when connecting to Codex.

## 0.5.0

- Rename the app and release bundles to Carrie On, preserving installed service identities and saved data.
- Add a coral C-arrow app icon, a native monochrome tray mark, and matching dashboard branding.

## 0.4.1

- Preserve a saved default Codex home when reopening from a different launch environment, avoiding unnecessary service restarts.

## 0.4.0

- Distribute a single Carrie On.app with Python, the native menu, and dashboard bundled.
- Configure login services on first launch, with native recovery dialogs for missing Codex or setup errors.
- Preserve settings and queue/history across app replacements; refresh services when the build changes.
- Keep application contents immutable after ad-hoc signing.
- Build releases through a single versioned command with automated tests, compatibility checks, archive verification, checksums, and release metadata.

## 0.3.0

- Bundle Python and the native menu for Apple Silicon with a macOS 13 deployment target.
- Install the runtime and separate app launchers through Install.command.
