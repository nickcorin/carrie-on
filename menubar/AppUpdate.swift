import AppKit
import Darwin
import Foundation

struct UpdateReply: Decodable {
    struct Release: Decodable { let version: String }
    let release: Release?
    let app: String?
}

struct UpdateFailure: Decodable, LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

enum AvailableUpdate {
    case release(UpdateReply.Release)
    case sourceCommit(String)

    var title: String {
        switch self {
        case .release(let release): return "Update to \(release.version)"
        case .sourceCommit(let commit): return "Update to \(commit.prefix(7))"
        }
    }
}

struct SourceBuild {
    enum Kind { case menu, bundledDev }
    let root: URL
    let python: URL
    let port: Int
    let kind: Kind
    let commit: String?

    init(bundle: Bundle) throws {
        guard let root = bundle.object(forInfoDictionaryKey: "CarrieOnSourceRoot") as? String,
              let python = bundle.object(forInfoDictionaryKey: "CarrieOnBuildPython") as? String,
              let rawPort = bundle.object(forInfoDictionaryKey: "CarrieOnPort") as? String,
              let port = Int(rawPort), (1024...65535).contains(port) else {
            throw UpdateFailure(message: "This build has no saved checkout or build interpreter. Install a fresh build from your checkout to enable Rebuild and Restart.")
        }
        self.root = URL(fileURLWithPath: root)
        self.python = URL(fileURLWithPath: python)
        self.port = port
        commit = bundle.object(forInfoDictionaryKey: "CarrieOnSourceCommit") as? String
        kind = bundle.object(forInfoDictionaryKey: "CarrieOnBuildProfile") as? String == "dev" ? .bundledDev : .menu
    }

    func checkoutCommit() throws -> String? {
        guard let commit, [40, 64].contains(commit.count), commit.allSatisfy({ $0.isHexDigit }) else { return nil }
        let head = try git(["rev-parse", "HEAD"])
        guard head.status == 0, [40, 64].contains(head.output.count),
              head.output.allSatisfy({ $0.isHexDigit }) else { return nil }
        guard head.output != commit else { return nil }
        let ancestry = try git(["merge-base", "--is-ancestor", commit, head.output])
        return ancestry.status == 0 ? head.output : nil
    }

    private func git(_ arguments: [String]) throws -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        process.currentDirectoryURL = root
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))
    }

    func prepare(at app: URL, output: @Sendable (String) async -> Void) async throws {
        let process = Process()
        process.executableURL = python
        process.currentDirectoryURL = root
        switch kind {
        case .menu:
            process.arguments = ["-u", root.appendingPathComponent("install_menubar.py").path,
                                 "--port", String(port), "--prepare-app", app.path]
        case .bundledDev:
            process.arguments = ["-u", root.appendingPathComponent("build_bundle.py").path,
                                 "--profile", "dev", "--output", app.deletingLastPathComponent().path,
                                 "--prepare-app", app.path]
        }
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        // Read complete lines so UTF-8 compiler diagnostics survive pipe chunk boundaries.
        var pending = Data()
        while true {
            let chunk = pipe.fileHandleForReading.availableData
            if chunk.isEmpty { break }
            pending.append(chunk)
            if let newline = pending.lastIndex(of: 10) {
                await output(String(decoding: pending[...newline], as: UTF8.self))
                pending.removeSubrange(...newline)
            }
        }
        if !pending.isEmpty { await output(String(decoding: pending, as: UTF8.self)) }
        process.waitUntilExit()
        guard process.terminationReason == .exit, process.terminationStatus == 0 else {
            throw UpdateFailure(message: "Build failed (exit \(process.terminationStatus)). Carrie On is still running. Fix the error above and try Rebuild and Restart again.")
        }
    }
}

@MainActor
final class BuildProgressWindow {
    private let window: NSWindow
    private let text = NSTextView()
    private let progress = NSProgressIndicator()

    init() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 680, height: 420),
                          styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "Rebuilding Carrie On"
        window.isReleasedWhenClosed = false
        window.standardWindowButton(.closeButton)?.isEnabled = false
        let content = window.contentView!
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        text.isEditable = false
        text.isSelectable = true
        text.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        text.textColor = .textColor
        text.backgroundColor = .textBackgroundColor
        text.textContainerInset = NSSize(width: 8, height: 8)
        text.frame = NSRect(x: 0, y: 0, width: 648, height: 360)
        text.isVerticallyResizable = true
        text.isHorizontallyResizable = false
        text.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        text.autoresizingMask = [.width]
        text.textContainer?.widthTracksTextView = true
        scroll.documentView = text
        progress.style = .bar
        progress.isIndeterminate = true
        for view in [scroll, progress] {
            view.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(view)
        }
        NSLayoutConstraint.activate([
            progress.topAnchor.constraint(equalTo: content.topAnchor, constant: 16),
            progress.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            progress.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            scroll.topAnchor.constraint(equalTo: progress.bottomAnchor, constant: 12),
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 16),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -16),
            scroll.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -16),
        ])
        progress.startAnimation(nil)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func append(_ output: String) {
        text.textStorage?.append(NSAttributedString(string: output, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular),
            .foregroundColor: NSColor.textColor,
        ]))
        text.scrollToEndOfDocument(nil)
    }

    func fail(_ error: Error) {
        append("\n\(error.localizedDescription)\n")
        window.title = "Carrie On couldn’t rebuild and restart"
        progress.stopAnimation(nil)
        progress.isHidden = true
        window.standardWindowButton(.closeButton)?.isEnabled = true
        window.makeKeyAndOrderFront(nil)
    }

    func close() { window.close() }
}

enum AppUpdate {
    static func check() throws -> AvailableUpdate? {
        if Bundle.main.object(forInfoDictionaryKey: "CarrieOnBuildProfile") as? String == "dev" {
            return try SourceBuild(bundle: .main).checkoutCommit().map(AvailableUpdate.sourceCommit)
        }
        return try run(["check"]).release.map(AvailableUpdate.release)
    }

    static func run(_ arguments: [String]) throws -> UpdateReply {
        let process = Process()
        process.executableURL = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/carrie_on")
        process.arguments = ["update"] + arguments
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            throw (try? JSONDecoder().decode(UpdateFailure.self, from: data)) ??
                UpdateFailure(message: "The update could not finish. Try again.")
        }
        return try JSONDecoder().decode(UpdateReply.self, from: data)
    }

    /// Exchange complete bundles atomically. The staged path becomes the recovery copy.
    /// If starting the replacement fails, exchange them back before reporting failure.
    static func install(candidate: URL, installed: URL, launch: (URL) throws -> Void) throws {
        func exchange() throws {
            guard renameatx_np(AT_FDCWD, candidate.path, AT_FDCWD, installed.path, UInt32(RENAME_SWAP)) == 0 else {
                throw UpdateFailure(message: "Could not replace Carrie On: \(String(cString: strerror(errno))). " +
                                    "Check that you can write to its Applications folder.")
            }
        }
        try exchange()
        do { try launch(installed) }
        catch {
            let launchError = error
            do { try exchange() }
            catch {
                throw UpdateFailure(message: "The update could not restart or restore the previous app. " +
                                    "Reopen Carrie On, or restore the app from \(candidate.path).")
            }
            throw launchError
        }
    }

    static func launchReplacement(_ app: URL) throws {
        let process = Process()
        process.executableURL = app.appendingPathComponent("Contents/MacOS/CarrieOnMenu")
        process.arguments = ["--relaunch-after", String(getpid())]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
    }

    /// Wait before duplicate-instance detection so the replacement survives the old menu.
    static func waitForPreviousInstance() throws {
        guard let index = CommandLine.arguments.firstIndex(of: "--relaunch-after") else { return }
        guard index + 1 < CommandLine.arguments.count,
              let pid = Int32(CommandLine.arguments[index + 1]), pid > 1, pid != getpid() else {
            throw UpdateFailure(message: "Invalid update restart request.")
        }
        for _ in 0..<300 {
            if kill(pid, 0) != 0 && errno == ESRCH { return }
            Thread.sleep(forTimeInterval: 0.1)
        }
        throw UpdateFailure(message: "The previous Carrie On instance did not quit. Reopen the app.")
    }
}

