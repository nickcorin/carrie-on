import Foundation

Task { @MainActor in
    let available = AppLifecycle(isStandalone: true) {
        .release(UpdateReply.Release(version: "0.12.0"))
    }
    await available.checkForUpdatesAtStartup()
    guard available.updateActionTitle == "Update to 0.12.0",
          !available.busy, available.updateStatus == nil else {
        fatalError("Startup did not quietly offer the discovered release")
    }

    let current = AppLifecycle(isStandalone: true) { nil }
    await current.checkForUpdatesAtStartup()
    guard current.updateActionTitle == "Check for Updates", current.availableUpdate == nil else {
        fatalError("An up-to-date app offered an update")
    }

    let offline = AppLifecycle(isStandalone: true) { throw UpdateFailure(message: "Offline") }
    await offline.checkForUpdatesAtStartup()
    guard offline.updateActionTitle == "Check for Updates", !offline.busy, offline.updateStatus == nil else {
        fatalError("Failed startup discovery blocked manual checking")
    }

    let source = AppLifecycle(isStandalone: false) { fatalError("A source build checked published releases") }
    await source.checkForUpdatesAtStartup()
    guard source.updateActionTitle == "Rebuild and Restart…" else {
        fatalError("Startup discovery changed source-build behavior")
    }
    let dev = AppLifecycle(isStandalone: true, isDevelopment: true) {
        .sourceCommit("abcdef1234567890abcdef1234567890abcdef1234")
    }
    await dev.checkForUpdatesAtStartup()
    guard dev.updateActionTitle == "Update to abcdef1", !dev.busy else {
        fatalError("Packaged dev build did not offer the newer local commit")
    }
    let currentDev = AppLifecycle(isStandalone: true, isDevelopment: true) { nil }
    await currentDev.checkForUpdatesAtStartup()
    guard currentDev.updateActionTitle == "Check for Updates" else {
        fatalError("Current dev build did not offer a manual update check")
    }
    print("Update discovery checks passed")
    exit(0)
}
RunLoop.main.run()
