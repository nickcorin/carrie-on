# Project instructions

Read [Architecture](docs/architecture.md) for component boundaries. Update the relevant [feature map](docs/features/index.md) when behavior changes.

## Local builds and testing

Carrie On.app is the release app. Carrie On Dev.app is the separate app for testing local changes. They have independent data, settings, queues, and background services and can run side by side.

Use `just dev` for local app testing. It builds the development packages in `dist/dev/`. Quit Carrie On Dev, replace Carrie On Dev.app with the app from the newly built DMG, and reopen that exact app. On first installation, install it alongside Carrie On.app.

Do not quit, replace, relaunch, or stop services for the release app unless the user explicitly requests work on that installation. Both apps use the `CarrieOnMenu` executable name, so do not stop processes by name alone. Identify the target by its full app path or bundle identifier. Dev uses `com.nickcorin.carrieon.dev.app`.

`just build`, `just package`, and `just ci` build the release profile. Use `just dev` to test changes interactively.
