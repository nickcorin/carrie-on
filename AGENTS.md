<!-- alexandria:start -->
Read /Users/nick/projects/nickcorin/alexandria/AGENTS.md before working in this project.
<!-- alexandria:end -->

## Local builds and testing

**Carrie On.app** is the live app installed from GitHub Releases. **Carrie On Dev.app** is the separate app for rebuilding and testing local changes. They have independent data, settings, queues, and background services and can run side by side.

Use `just dev` for local app testing; it builds the development artifacts in `dist/dev/`. Quit only **Carrie On Dev**, replace only **Carrie On Dev.app** with the app from the newly built DMG, and reopen that exact app. On first installation, install it alongside **Carrie On.app**. See [the development app instructions](RELEASING.md#local-development-app) for installation and isolation details.

Do not quit, replace, relaunch, or stop services for the live **Carrie On** app when rebuilding or testing local changes unless the user explicitly requests work on that installation. Avoid broad process-name kills: both apps use the `CarrieOnMenu` executable name. Identify the target by its full app path or bundle identifier (`com.nickcorin.carrieon.dev.app` for Dev).

`just build`, `just package`, and `just ci` build the release profile; their output is not the development app. Use `just dev` when you need to run local changes interactively.
