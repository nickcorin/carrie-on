import AppKit
import Foundation

struct SetupResult: Decodable {
    let version: String
    let port: Int
    let started: Bool
}

struct SetupFailure: Decodable, LocalizedError {
    let code: String
    let message: String
    var errorDescription: String? { message }
}

enum AppSetup {
    static var isDevelopment: Bool {
        Bundle.main.object(forInfoDictionaryKey: "CarrieOnBuildProfile") as? String == "dev"
    }

    static var isStandalone: Bool {
        Bundle.main.object(forInfoDictionaryKey: "CarrieOnStandalone") as? Bool == true
    }

    static func run(arguments: [String] = []) throws -> SetupResult {
        let process = Process()
        process.executableURL = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/carrie_on")
        process.arguments = ["setup", "--json"] + arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        // Drain before waiting so even a long diagnostic cannot fill the pipe.
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw (try? JSONDecoder().decode(SetupFailure.self, from: data)) ??
                SetupFailure(code: "setup_failed", message: "Setup could not finish. Reopen Carrie On to retry.")
        }
        let result = try JSONDecoder().decode(SetupResult.self, from: data)
        guard (1024...65535).contains(result.port) else {
            throw SetupFailure(code: "setup_failed", message: "Setup returned an invalid local API port.")
        }
        return result
    }

    @MainActor
    static func recovery(for error: Error) -> [String]? {
        let alert = NSAlert()
        alert.messageText = "Carrie On needs setup"
        alert.informativeText = error.localizedDescription
        if let failure = error as? SetupFailure, failure.code == "move_app" {
            alert.messageText = "Move Carrie On to Applications"
            alert.addButton(withTitle: "Open Applications")
            alert.addButton(withTitle: "Quit")
            NSApp.activate(ignoringOtherApps: true)
            if alert.runModal() == .alertFirstButtonReturn {
                let applications = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications")
                try? FileManager.default.createDirectory(at: applications, withIntermediateDirectories: true)
                NSWorkspace.shared.open(applications)
                NSWorkspace.shared.activateFileViewerSelecting([Bundle.main.bundleURL])
            }
            return nil
        }
        if let failure = error as? SetupFailure, failure.code == "codex_missing" {
            alert.messageText = "Connect Carrie On to Codex"
            alert.addButton(withTitle: "Choose Codex…")
            alert.addButton(withTitle: "Retry")
            alert.addButton(withTitle: "Quit")
            NSApp.activate(ignoringOtherApps: true)
            switch alert.runModal() {
            case .alertFirstButtonReturn:
                let panel = NSOpenPanel()
                panel.title = "Choose the Codex command-line executable"
                panel.canChooseDirectories = false
                panel.allowsMultipleSelection = false
                panel.showsHiddenFiles = true
                guard panel.runModal() == .OK, let url = panel.url else { return nil }
                return ["--codex", url.path]
            case .alertSecondButtonReturn: return []
            default: return nil
            }
        }
        alert.addButton(withTitle: "Retry")
        alert.addButton(withTitle: "Quit")
        NSApp.activate(ignoringOtherApps: true)
        return alert.runModal() == .alertFirstButtonReturn ? ["--repair"] : nil
    }
}
