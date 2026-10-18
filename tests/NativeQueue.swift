import Foundation

@MainActor
func testNativeQueue() async throws {
    let baseURL = URL(string: CommandLine.arguments[1])!
    let python = CommandLine.arguments[2]
    let configuration = URLSessionConfiguration.ephemeral
    configuration.connectionProxyDictionary = [:]
    let session = URLSession(configuration: configuration)
    defer { session.invalidateAndCancel() }
    let model = ChatPickerModel(baseURL: baseURL, session: session, recoveryDefaults: nil, didQueue: {})
    model.ready = true
    model.connection = .connected
    let chat = PickerChat(id: "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa", title: "Shared queue", cwd: nil)
    model.instructions[chat.id] = "Finish café tests\nKeep this line."

    func cli(_ arguments: [String]) throws -> [String: Any] {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "carrie_on", "--server", baseURL.absoluteString, "--json"] + arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        try process.run()
        let output = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { fatalError("CLI failed: \(arguments)") }
        return try JSONSerialization.jsonObject(with: output) as! [String: Any]
    }

    func settle() async throws {
        for _ in 0..<500 {
            if !model.working && !model.checkingQueue { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        fatalError("Native queue did not settle")
    }

    // The test server commits creation, then returns an unavailable response.
    guard await model.queueChat(chat) == false else { fatalError("Expected an uncertain creation") }
    try await Task.sleep(nanoseconds: 20_000_000)
    try await settle()
    await model.updateQueue(refreshMetadata: false)
    guard let original = model.queuedEntries.first, model.canAct(on: chat.id) else {
        fatalError("Native creation did not recover through the real HTTP service")
    }
    let persisted = try cli(["queue", "show", original.enrollmentID])
    guard persisted["instruction"] as? String == "Finish café tests\nKeep this line.",
          persisted["revision"] as? Int == 1 else { fatalError("CLI did not read the native creation") }

    _ = try cli(["queue", "edit", original.enrollmentID, "--instruction", "Saved by CLI"])
    guard await model.changeQueued(original, change: .instruction("Stale native edit", expected: original.prompt)) == false else {
        fatalError("Native stale edit overwrote the CLI edit")
    }
    try await Task.sleep(nanoseconds: 20_000_000)
    try await settle()
    await model.updateQueue(refreshMetadata: false)
    guard let refreshed = model.queuedEntries.first, refreshed.prompt == "Saved by CLI", refreshed.revision == 2 else {
        fatalError("Native refresh did not recover the CLI revision")
    }
    guard await model.changeQueued(refreshed, change: .instruction("Saved by native", expected: refreshed.prompt)) else {
        fatalError("Native edit failed against the real HTTP service")
    }
    try await Task.sleep(nanoseconds: 20_000_000)
    try await settle()
    let edited = try cli(["queue", "show", original.enrollmentID])
    guard edited["instruction"] as? String == "Saved by native", edited["revision"] as? Int == 3,
          let current = model.queuedEntries.first else { fatalError("Native edit was not persisted") }
    guard await model.changeQueued(current, change: .remove) else { fatalError("Native removal failed") }
    let removed = try cli(["queue", "show", original.enrollmentID])
    guard removed["state"] as? String == "cancelled" else { fatalError("CLI did not observe native removal") }
    print("Native and CLI queue operations passed through FastAPI and SQLite")
}

Task { @MainActor in
    do {
        try await testNativeQueue()
        exit(0)
    } catch {
        fputs("\(error)\n", stderr)
        exit(1)
    }
}
RunLoop.main.run()
