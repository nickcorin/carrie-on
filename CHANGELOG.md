# Changelog

## 0.5.1

- Add GitHub CI and verified draft releases for Apple Silicon Macs.
- Validate release tags against the version and changelog before building.
- Include the MIT license and complete user documentation in release bundles.
- Report the current app version when connecting to Codex.

## 0.5.0

- Rename the app and release bundles to Carrie On, preserving installed service identities and saved data.
- Add a coral C-arrow app icon, a native monochrome tray mark, and matching dashboard branding.

## 0.4.1

- Preserve a saved default Codex home when reopening from a different launch environment, avoiding unnecessary service restarts.

## 0.4.0

- Distribute a single Tokenmaxxer.app with Python, the native menu, and dashboard bundled.
- Configure login services on first launch, with native recovery dialogs for missing Codex or setup errors.
- Preserve settings and queue/history across app replacements; refresh services when the build changes.
- Keep application contents immutable after ad-hoc signing.
- Build releases through a single versioned command with automated tests, compatibility checks, archive verification, checksums, and release metadata.

## 0.3.0

- Bundle Python and the native menu for Apple Silicon with a macOS 13 deployment target.
- Install the runtime and separate app launchers through Install.command.
