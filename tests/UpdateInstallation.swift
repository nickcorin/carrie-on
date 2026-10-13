import Foundation

if CommandLine.arguments.contains("--relaunch-after") {
    try AppUpdate.waitForPreviousInstance()
    print("Previous instance exited")
    exit(0)
}

let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
defer { try? FileManager.default.removeItem(at: root) }
let installed = root.appendingPathComponent("Applications/Carrie On.app")
let candidate = root.appendingPathComponent("Applications/.update/Carrie On.app")
for (app, version) in [(installed, "old"), (candidate, "new")] {
    try FileManager.default.createDirectory(at: app, withIntermediateDirectories: true)
    try Data(version.utf8).write(to: app.appendingPathComponent("version"))
}
func version(_ app: URL) throws -> String { try String(contentsOf: app.appendingPathComponent("version"), encoding: .utf8) }

try AppUpdate.install(candidate: candidate, installed: installed) { app in
    guard try version(app) == "new" else { fatalError("Launch saw an incomplete replacement") }
}
guard try version(installed) == "new", try version(candidate) == "old" else { fatalError("Update lost the recovery copy") }

// The same primitive restores the old app when launching a replacement fails.
do {
    try AppUpdate.install(candidate: candidate, installed: installed) { _ in throw UpdateFailure(message: "launch failed") }
    fatalError("A failed launch was accepted")
} catch let error as UpdateFailure {
    guard error.message == "launch failed" else { throw error }
}
guard try version(installed) == "new", try version(candidate) == "old" else { fatalError("Rollback lost an app") }
try AppUpdate.install(candidate: candidate, installed: installed) { _ in }
guard try version(installed) == "old", try version(candidate) == "new" else { fatalError("Retry failed") }
do {
    try AppUpdate.install(candidate: root.appendingPathComponent("missing"), installed: installed) { _ in fatalError("Missing app launched") }
    fatalError("Missing candidate accepted")
} catch is UpdateFailure {}
guard try version(installed) == "old" else { fatalError("Failed exchange changed the app") }
print("Update installation checks passed")
