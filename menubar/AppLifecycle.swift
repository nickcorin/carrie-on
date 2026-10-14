import AppKit
import Combine
import Darwin
import Foundation

struct LifecycleFiles {
    static let labels = Bundle.main.object(forInfoDictionaryKey: "CarrieOnServiceLabels") as? [String]
        ?? ["com.nickcorin.carrieon", "com.nickcorin.carrieon.ui", "com.nickcorin.carrieon.menu"]
    static let dataDirectory = Bundle.main.object(forInfoDictionaryKey: "CarrieOnDataDirectory") as? String ?? "Carrie On"
    let home: URL
    var data: URL { home.appendingPathComponent("Library/Application Support/\(Self.dataDirectory)") }
    private var root: URL { data.appendingPathComponent("Lifecycle") }
    private var preferences: URL { root.appendingPathComponent("preferences.plist") }

    func savedAgent(_ label: String) -> URL { root.appendingPathComponent("Agents/\(label).plist") }
    func loginAgent(_ label: String) -> URL { home.appendingPathComponent("Library/LaunchAgents/\(label).plist") }

    private func write(_ data: Data, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }

    /// Reconcile all three login registrations without changing running services.
    /// Nil preserves the saved preference. Repeating repairs partial changes.
    func reconcile(launchAtLogin requested: Bool? = nil) throws -> Bool {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let descriptor = Darwin.open(root.appendingPathComponent("lock").path, O_CREAT | O_RDWR, 0o600)
        guard descriptor >= 0 else { throw failure("Could not open startup settings.") }
        defer { Darwin.close(descriptor) }
        guard flock(descriptor, LOCK_EX) == 0 else { throw failure("Could not lock startup settings.") }
        defer { flock(descriptor, LOCK_UN) }
        var enabled = true
        if FileManager.default.fileExists(atPath: preferences.path) {
            let raw = try PropertyListSerialization.propertyList(from: Data(contentsOf: preferences), format: nil)
            guard let saved = raw as? [String: Any], let value = saved["launch_at_login"] as? Bool
            else { throw failure("Invalid startup settings.") }
            enabled = value
        }
        if let requested {
            enabled = requested
            let data = try PropertyListSerialization.data(fromPropertyList: ["launch_at_login": enabled], format: .xml, options: 0)
            try write(data, to: preferences)
        }
        for label in Self.labels {
            let login = loginAgent(label)
            let saved = savedAgent(label)
            let source = FileManager.default.fileExists(atPath: login.path) ? login : saved
            guard FileManager.default.fileExists(atPath: source.path) else { continue }
            let data = try Data(contentsOf: source)
            let config = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any]
            guard config?["Label"] as? String == label else { throw failure("Unexpected startup service: \(label)") }
            try write(data, to: saved)
            if enabled { try write(data, to: login) }
            else if FileManager.default.fileExists(atPath: login.path) { try FileManager.default.removeItem(at: login) }
        }
        return enabled
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "Carrie On", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}

enum LifecycleServices {
    static func launchctl(_ arguments: [String]) throws -> (Int32, String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    /// Start or stop only Carrie On's worker and interface. The app exits itself.
    static func setRunning(_ running: Bool, files: LifecycleFiles,
                           run: ([String]) throws -> (Int32, String) = launchctl,
                           attempts: Int = 100,
                           pause: () -> Void = { Thread.sleep(forTimeInterval: 0.1) }) throws {
        let domain = "gui/\(getuid())"
        for label in LifecycleFiles.labels.prefix(2) {
            let target = "\(domain)/\(label)"
            let loaded = try run(["print", target]).0 == 0
            if loaded == running { continue }
            let arguments = running ? ["bootstrap", domain, files.savedAgent(label).path] : ["bootout", target]
            let result = try run(arguments)
            var nowLoaded = try run(["print", target]).0 == 0
            // launchd acknowledges bootout before the exiting service disappears.
            for _ in 1..<max(1, attempts) {
                if nowLoaded == running { break }
                pause()
                nowLoaded = try run(["print", target]).0 == 0
            }
            guard nowLoaded == running else {
                throw NSError(domain: "Carrie On", code: result.0 == 0 ? Int(ETIMEDOUT) : Int(result.0), userInfo: [NSLocalizedDescriptionKey:
                    "Timed out waiting to \(running ? "start" : "stop") \(label). \(result.1)"])
            }
        }
    }
}

func lifecycleSelfTest() throws {
    let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: home) }
    let files = LifecycleFiles(home: home)
    for label in LifecycleFiles.labels {
        let path = files.loginAgent(label)
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        try PropertyListSerialization.data(fromPropertyList: ["Label": label, "ProgramArguments": ["/fixture"]],
                                           format: .xml, options: 0).write(to: path)
    }
    guard try files.reconcile(launchAtLogin: false) == false else { fatalError("Startup toggle was not saved") }
    for label in LifecycleFiles.labels {
        guard !FileManager.default.fileExists(atPath: files.loginAgent(label).path),
              FileManager.default.fileExists(atPath: files.savedAgent(label).path)
        else { fatalError("Disabled startup lost manual launch or kept login registration") }
    }
    guard try files.reconcile() == false else { fatalError("Manual launch reset the startup preference") }
    var loaded: Set<String> = []
    var launches = 0
    let run: ([String]) throws -> (Int32, String) = { args in
        switch args[0] {
        case "print": return (loaded.contains(args[1].components(separatedBy: "/").last!) ? 0 : 113, "")
        case "bootstrap":
            loaded.insert(URL(fileURLWithPath: args[2]).deletingPathExtension().lastPathComponent)
            launches += 1
            return (0, "")
        case "bootout":
            loaded.remove(args[1].components(separatedBy: "/").last!)
            return (0, "")
        default: fatalError("Unexpected lifecycle command")
        }
    }
    try LifecycleServices.setRunning(true, files: files, run: run)
    try LifecycleServices.setRunning(true, files: files, run: run)
    guard loaded == Set(LifecycleFiles.labels.prefix(2)), launches == 2 else { fatalError("Manual launch duplicated services") }
    try LifecycleServices.setRunning(false, files: files, run: run)
    try LifecycleServices.setRunning(false, files: files, run: run)
    guard loaded.isEmpty else { fatalError("Quit left background services running") }
    var remainingChecks = 0
    var delayedLoaded = true
    var stops = 0
    let delayedStop: ([String]) throws -> (Int32, String) = { args in
        switch args[0] {
        case "bootout":
            stops += 1
            remainingChecks = 3
            return (0, "")
        case "print":
            if remainingChecks > 0 {
                remainingChecks -= 1
                if remainingChecks == 0 { delayedLoaded = false }
            }
            return (delayedLoaded ? 0 : 113, "")
        default: fatalError("Unexpected shutdown command")
        }
    }
    try LifecycleServices.setRunning(false, files: files, run: delayedStop, attempts: 5, pause: {})
    guard !delayedLoaded, stops == 1 else { fatalError("Asynchronous shutdown was not awaited") }
    do {
        try LifecycleServices.setRunning(false, files: files, run: { _ in (0, "") }, attempts: 3, pause: {})
        fatalError("A service that never stopped was accepted")
    } catch let error as NSError {
        guard error.code == Int(ETIMEDOUT) else { throw error }
    }
    guard try files.reconcile(launchAtLogin: true) else { fatalError("Startup could not be enabled") }
    for label in LifecycleFiles.labels {
        guard FileManager.default.fileExists(atPath: files.loginAgent(label).path)
        else { fatalError("Startup registration was not restored") }
    }
}

@MainActor
final class AppLifecycle: ObservableObject {
    @Published private(set) var launchAtLogin = true
    @Published private(set) var busy = false
    @Published private(set) var updateStatus: String?
    @Published private(set) var ready = false
    @Published private(set) var availableUpdate: AvailableUpdate?
    private var updateCheck: Task<AvailableUpdate?, Error>?
    private let isStandalone: Bool
    private let isDevelopment: Bool
    private let checkUpdate: @Sendable () throws -> AvailableUpdate?
    private var rebuildWindow: BuildProgressWindow?

    init(isStandalone: Bool = AppSetup.isStandalone,
         isDevelopment: Bool = AppSetup.isDevelopment,
         checkUpdate: @escaping @Sendable () throws -> AvailableUpdate? = { try AppUpdate.check() }) {
        self.isStandalone = isStandalone
        self.isDevelopment = isDevelopment
        self.checkUpdate = checkUpdate
    }

    var updateActionTitle: String {
        if let update = availableUpdate { return update.title }
        guard isStandalone else { return "Rebuild and Restart…" }
        return "Check for Updates"
    }

    func start() async {
        busy = true
        defer { busy = false }
        do {
            launchAtLogin = try await Task.detached {
                let files = LifecycleFiles(home: FileManager.default.homeDirectoryForCurrentUser)
                let enabled = try files.reconcile()
                try LifecycleServices.setRunning(true, files: files)
                return enabled
            }.value
            ready = true
        } catch { show(error) }
    }

    func setLaunchAtLogin(_ enabled: Bool) {
        guard !busy else { return }
        busy = true
        Task {
            defer { busy = false }
            do {
                launchAtLogin = try await Task.detached {
                    try LifecycleFiles(home: FileManager.default.homeDirectoryForCurrentUser).reconcile(launchAtLogin: enabled)
                }.value
            } catch { show(error) }
        }
    }

    func quit() {
        guard !busy else { return }
        busy = true
        Task {
            defer { busy = false }
            do {
                try await Task.detached {
                    try LifecycleServices.setRunning(false, files: LifecycleFiles(home: FileManager.default.homeDirectoryForCurrentUser))
                }.value
                NSApp.terminate(nil)
            } catch { show(error) }
        }
    }

    private func show(_ error: Error) {
        let alert = NSAlert()
        alert.messageText = "Carrie On couldn’t finish"
        alert.informativeText = error.localizedDescription
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}

extension AppLifecycle {
    func checkForUpdatesAtStartup() async {
        guard isStandalone else { return }
        // Startup discovery is silent and does not block other lifecycle actions.
        _ = try? await findAvailableUpdate()
    }

    private func findAvailableUpdate() async throws -> AvailableUpdate? {
        if let check = updateCheck { return try await check.value }
        let check = Task.detached { [checkUpdate] in try checkUpdate() }
        updateCheck = check
        defer { updateCheck = nil }
        let update = try await check.value
        availableUpdate = update
        return update
    }

    func checkForUpdates() {
        guard !busy, ready else { return }
        if !isStandalone {
            rebuildAndRestart()
            return
        }
        if isDevelopment {
            checkDevelopmentUpdates()
            return
        }
        busy = true
        Task {
            defer { busy = false; updateStatus = nil }
            do {
                let metadataURL = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/release.json")
                let originalMetadata = try Data(contentsOf: metadataURL)
                var update = availableUpdate
                if update == nil {
                    updateStatus = "Checking for Updates…"
                    update = try await findAvailableUpdate()
                }
                guard case .release(let release) = update else {
                    showUpdate(message: "No update available", detail: "You’re running Carrie On \(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""). No newer published stable release is available.")
                    return
                }
                let alert = NSAlert()
                alert.messageText = "Carrie On \(release.version) is available"
                alert.informativeText = "Download and install this update? Carrie On will restart. Your queue and settings will be preserved."
                alert.addButton(withTitle: "Install and Restart")
                alert.addButton(withTitle: "Not Now")
                alert.addButton(withTitle: "Release Notes")
                NSApp.activate(ignoringOtherApps: true)
                let choice = alert.runModal()
                if choice == .alertThirdButtonReturn {
                    NSWorkspace.shared.open(URL(string: "https://github.com/nickcorin/carrie-on/releases/tag/v\(release.version)")!)
                    return
                }
                guard choice == .alertFirstButtonReturn else { return }
                updateStatus = "Downloading Update…"
                let prepared = try await Task.detached {
                    try AppUpdate.run(["prepare", "--version", release.version])
                }.value
                guard let path = prepared.app else { throw UpdateFailure(message: "The update did not provide an app to install.") }
                let candidate = URL(fileURLWithPath: path)
                updateStatus = "Installing Update…"
                do {
                    try await Task.detached {
                        let files = LifecycleFiles(home: FileManager.default.homeDirectoryForCurrentUser)
                        let lockURL = files.data.appendingPathComponent("install.lock")
                        let lock = Darwin.open(lockURL.path, O_CREAT | O_RDWR, 0o600)
                        guard lock >= 0 else { throw UpdateFailure(message: "Could not lock the installation. Reopen Carrie On and retry.") }
                        defer { Darwin.close(lock) }
                        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else {
                            throw UpdateFailure(message: "Another installation is in progress. Try again when it finishes.")
                        }
                        defer { flock(lock, LOCK_UN) }
                        guard try Data(contentsOf: metadataURL) == originalMetadata else {
                            throw UpdateFailure(message: "The installed app changed during the download. Reopen it and check for updates again.")
                        }
                        do {
                            try LifecycleServices.setRunning(false, files: files)
                            try AppUpdate.install(candidate: candidate, installed: Bundle.main.bundleURL,
                                                  launch: AppUpdate.launchReplacement)
                        } catch {
                            // Reopening also repairs services if recovery itself fails.
                            try? LifecycleServices.setRunning(true, files: files)
                            throw error
                        }
                    }.value
                } catch {
                    // Retain staging on failure: it may hold the only recovery copy.
                    throw UpdateFailure(message: "\(error.localizedDescription)\n\nStaged or recovery app: \(path)")
                }
                NSApp.terminate(nil)
            } catch { showUpdate(message: "Carrie On couldn’t update", detail: error.localizedDescription) }
        }
    }

    private func checkDevelopmentUpdates() {
        busy = true
        updateStatus = "Checking for Updates…"
        Task {
            do {
                let update = try await findAvailableUpdate()
                busy = false
                updateStatus = nil
                if case .sourceCommit = update {
                    rebuildAndRestart()
                } else {
                    showUpdate(message: "No update available", detail: "Carrie On Dev is not behind the local checkout’s HEAD.")
                }
            } catch {
                busy = false
                updateStatus = nil
                showUpdate(message: "Carrie On couldn’t check for updates", detail: error.localizedDescription)
            }
        }
    }

    private func rebuildAndRestart() {
        let build: SourceBuild
        do { build = try SourceBuild(bundle: .main) }
        catch { showUpdate(message: "Carrie On couldn’t rebuild", detail: error.localizedDescription); return }
        let alert = NSAlert()
        alert.messageText = "Rebuild Carrie On?"
        alert.informativeText = "Build and restart using the code in this checkout. Your queue and settings will be preserved.\n\n\(build.root.path)"
        alert.addButton(withTitle: "Rebuild and Restart")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        busy = true
        updateStatus = "Rebuilding…"
        rebuildWindow?.close()
        let progress = BuildProgressWindow()
        rebuildWindow = progress
        let installed = Bundle.main.bundleURL
        let staging = installed.deletingLastPathComponent().appendingPathComponent(".Carrie-On-rebuild-\(UUID().uuidString)")
        let candidate = staging.appendingPathComponent(installed.lastPathComponent)
        Task {
            defer { busy = false; updateStatus = nil }
            do {
                progress.append("Building from \(build.root.path)\n\n")
                do {
                    try await Task.detached {
                        try await build.prepare(at: candidate) { output in
                            await progress.append(output)
                        }
                    }.value
                } catch {
                    try? FileManager.default.removeItem(at: staging)
                    throw error
                }
                updateStatus = "Restarting…"
                progress.append("\nBuild succeeded. Restarting Carrie On…\n")
                try await Task.detached {
                    let files = LifecycleFiles(home: FileManager.default.homeDirectoryForCurrentUser)
                    do {
                        try LifecycleServices.setRunning(false, files: files)
                        try AppUpdate.install(candidate: candidate, installed: installed, launch: AppUpdate.launchReplacement)
                    } catch {
                        try? LifecycleServices.setRunning(true, files: files)
                        throw UpdateFailure(message: "\(error.localizedDescription)\n\nStaged or recovery app: \(candidate.path)")
                    }
                }.value
                try? FileManager.default.removeItem(at: staging)
                progress.close()
                NSApp.terminate(nil)
            } catch { progress.fail(error) }
        }
    }

    private func showUpdate(message: String, detail: String) {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = detail
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}
