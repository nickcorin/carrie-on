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

enum AppUpdate {
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

