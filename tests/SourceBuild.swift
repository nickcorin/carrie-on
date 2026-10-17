import Foundation

actor BuildOutput {
    var text = ""
    func append(_ chunk: String) { text += chunk }
}

@main
struct SourceBuildChecks {
    static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("source build \(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let installed = root.appendingPathComponent("Menu.app")
        let contents = installed.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        let interpreter = root.appendingPathComponent("python")
        try Data("#!/bin/sh\ntest \"$1\" = -u && test \"$2\" = -m || exit 8\nmodule=\"$3\"\nshift 3\nexec /bin/sh \"$module\" \"$@\"\n".utf8).write(to: interpreter)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: interpreter.path)
        let metadata: [String: Any] = ["CFBundleIdentifier": "test.source-build", "CFBundlePackageType": "APPL",
                                      "CarrieOnSourceRoot": root.path, "CarrieOnBuildPython": interpreter.path, "CarrieOnPort": "8877"]
        try PropertyListSerialization.data(fromPropertyList: metadata, format: .xml, options: 0)
            .write(to: contents.appendingPathComponent("Info.plist"))
        try Data("old".utf8).write(to: installed.appendingPathComponent("version"))
        let bundle = Bundle(url: installed)!
        let build = try SourceBuild(bundle: bundle)
        let script = root.appendingPathComponent("tools.install_menubar")
        let candidate = root.appendingPathComponent("staging/Menu.app")
        // More than a pipe buffer of output must be drained before waiting for exit.
        let diagnostic = String(repeating: "compiler diagnostic é\n", count: 5000)
        try Data(diagnostic.utf8).write(to: root.appendingPathComponent("diagnostic"))
        try Data("cat diagnostic\necho 'compile failed' >&2\nexit 23\n".utf8).write(to: script)
        let failedOutput = BuildOutput()
        do {
            try await build.prepare(at: candidate) { await failedOutput.append($0) }
            fatalError("Failed compilation was accepted")
        } catch let failure as UpdateFailure {
            guard failure.message.contains("exit 23") else { throw failure }
        }
        let failureText = await failedOutput.text
        guard failureText == diagnostic + "compile failed\n",
              try String(contentsOf: installed.appendingPathComponent("version"), encoding: .utf8) == "old"
        else { fatalError("Build failure lost diagnostics or changed the installed app") }

        try Data("test \"$1\" = --port && test \"$2\" = 8877 && test \"$3\" = --prepare-app || exit 9\nmkdir -p \"$4\"\nprintf new > \"$4/version\"\nprintf 'Build ready\\n'\n".utf8).write(to: script)
        let successfulOutput = BuildOutput()
        try await build.prepare(at: candidate) { await successfulOutput.append($0) }
        guard await successfulOutput.text == "Build ready\n",
              try String(contentsOf: installed.appendingPathComponent("version"), encoding: .utf8) == "old"
        else { fatalError("Staging changed the running app") }
        try AppUpdate.install(candidate: candidate, installed: installed) { app in
            guard try String(contentsOf: app.appendingPathComponent("version"), encoding: .utf8) == "new"
            else { fatalError("Restart did not receive the successful build") }
        }
        guard try String(contentsOf: installed.appendingPathComponent("version"), encoding: .utf8) == "new"
        else { fatalError("Successful build was not installed") }

        let dev = root.appendingPathComponent("Dev.app/Contents")
        try FileManager.default.createDirectory(at: dev, withIntermediateDirectories: true)
        var devMetadata = metadata
        devMetadata["CFBundleIdentifier"] = "test.bundled-dev"
        devMetadata["CarrieOnBuildProfile"] = "dev"
        devMetadata["CarrieOnStandalone"] = true
        try PropertyListSerialization.data(fromPropertyList: devMetadata, format: .xml, options: 0)
            .write(to: dev.appendingPathComponent("Info.plist"))
        let devBuild = try SourceBuild(bundle: Bundle(url: dev.deletingLastPathComponent())!)
        let devScript = root.appendingPathComponent("tools.build_bundle")
        let devCandidate = root.appendingPathComponent("dev-staging/Dev.app")
        try Data("echo 'backend build failed' >&2\nexit 42\n".utf8).write(to: devScript)
        let devFailure = BuildOutput()
        do {
            try await devBuild.prepare(at: devCandidate) { await devFailure.append($0) }
            fatalError("Failed backend build was accepted")
        } catch let failure as UpdateFailure {
            guard failure.message.contains("exit 42") else { throw failure }
        }
        guard await devFailure.text == "backend build failed\n" else { fatalError("Backend diagnostics were lost") }
        try Data("test \"$1\" = --profile && test \"$2\" = dev && test \"$3\" = --output && test \"$5\" = --prepare-app || exit 9\nmkdir -p \"$6/Contents/MacOS\"\nprintf 'new backend' > \"$6/Contents/MacOS/carrie_on\"\nprintf 'Verified dev build\\n'\n".utf8).write(to: devScript)
        let devOutput = BuildOutput()
        try await devBuild.prepare(at: devCandidate) { await devOutput.append($0) }
        try AppUpdate.install(candidate: devCandidate, installed: dev.deletingLastPathComponent()) { app in
            guard try String(contentsOf: app.appendingPathComponent("Contents/MacOS/carrie_on"), encoding: .utf8) == "new backend"
            else { fatalError("Dev restart did not receive the rebuilt backend") }
        }
        guard await devOutput.text == "Verified dev build\n" else { fatalError("Dev build used the source menu builder") }
        print("Source rebuild checks passed")
    }
}
