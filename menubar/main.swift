import AppKit
import Foundation

struct UsageWindow: Decodable {
    let bucket: String
    let name: String
    let usedPercent: Double
    let resetsAt: Double
    let windowMinutes: Int?

    var remainingPercent: Int { max(0, min(100, 100 - Int(usedPercent.rounded()))) }

    var label: String {
        guard let minutes = windowMinutes, minutes > 0 else { return "\(bucket) · \(name)" }
        let duration = minutes % 1440 == 0 ? "\(minutes / 1440)-day" :
            minutes % 60 == 0 ? "\(minutes / 60)-hour" : "\(minutes)-minute"
        return "\(bucket) · \(duration)"
    }
}

struct UsageSummary: Decodable {
    let windows: [UsageWindow]

    var lowestRemaining: Int? {
        windows.map(\.remainingPercent).min()
    }

    func nextReset(after date: Date) -> Date? {
        windows.map { Date(timeIntervalSince1970: $0.resetsAt) }.filter { $0 > date }.min()
    }

    func resetCountdown(at date: Date) -> String {
        guard let reset = nextReset(after: date) else { return "due" }
        let seconds = reset.timeIntervalSince(date)
        if seconds < 60 { return "<1m" }

        let minutes = Int(ceil(seconds / 60))
        let hours = minutes / 60
        if hours >= 24 {
            let remainder = hours % 24
            return "\(hours / 24)d" + (remainder > 0 ? " \(remainder)h" : "")
        }
        if hours > 0 {
            let remainder = minutes % 60
            return "\(hours)h" + (remainder > 0 ? " \(remainder)m" : "")
        }
        return "\(minutes)m"
    }
}

struct QueueSummary: Decodable {
    let count: Int
    let paused: Bool
    let needsAttention: Int
}

struct MenuSnapshot: Decodable {
    let connected: Bool?
    let usage: UsageSummary?
    let queue: QueueSummary?
    let usageError: String?
    let queueError: String?

    static func decode(_ data: Data) throws -> MenuSnapshot {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let snapshot = try decoder.decode(MenuSnapshot.self, from: data)
        if let usage = snapshot.usage {
            guard !usage.windows.isEmpty, usage.windows.allSatisfy({
                $0.usedPercent.isFinite && $0.usedPercent >= 0 && $0.usedPercent <= 10000 &&
                $0.resetsAt.isFinite && $0.resetsAt > 0
            }) else { throw MenuError.invalidData }
        }
        if let queue = snapshot.queue, queue.count < 0 || queue.needsAttention < 0 {
            throw MenuError.invalidData
        }
        return snapshot
    }

    func compactTitle(at date: Date) -> String {
        let count = queue.map { String($0.count) } ?? "—"
        let countdown = usage?.resetCountdown(at: date) ?? "—"
        return "\(count) · \(countdown)"
    }
}

enum MenuError: LocalizedError {
    case invalidData, unavailable
    var errorDescription: String? {
        switch self {
        case .invalidData: return "Local service returned incomplete data."
        case .unavailable: return "Local service unavailable. Retrying automatically."
        }
    }
}

@MainActor
final class MenuApp: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var baseURL: URL
    private let diagnosticsURL: URL?
    private var timer: Timer?
    private var queueTimer: Timer?
    private var snapshot: MenuSnapshot? { picker?.model.summary }
    private var refreshing = false
    private var picker: ChatPickerPopover!
    private let lifecycle = AppLifecycle()

    init(baseURL: URL, diagnosticsURL: URL?) {
        self.baseURL = baseURL
        self.diagnosticsURL = diagnosticsURL
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let identifier = Bundle.main.bundleIdentifier,
           NSRunningApplication.runningApplications(withBundleIdentifier: identifier).count > 1 {
            NSApp.terminate(nil)
            return
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = Brand.trayIcon
        statusItem.button?.imagePosition = .imageLeading
        statusItem.button?.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePicker)
        if AppSetup.isStandalone {
            statusItem.button?.title = " Starting…"
            statusItem.button?.isEnabled = false
            startSetup()
        } else {
            startMenu()
        }
    }

    private func startSetup(arguments: [String] = []) {
        Task {
            do {
                let result = try await Task.detached { try AppSetup.run(arguments: arguments) }.value
                baseURL = URL(string: "http://127.0.0.1:\(result.port)")!
                statusItem.button?.isEnabled = true
                startMenu()
            } catch {
                if let retry = AppSetup.recovery(for: error) { startSetup(arguments: retry) }
                else { NSApp.terminate(nil) }
            }
        }
    }

    private func startMenu() {
        picker = ChatPickerPopover(baseURL: baseURL, lifecycle: lifecycle, didQueue: { [weak self] in self?.refresh() },
                                   visibilityChanged: { [weak self] in self?.writeDiagnostics() })
        render()
        refresh()
        Task {
            await lifecycle.start()
            refresh()
        }
        timer = Timer(timeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.render()
                self?.refresh()
            }
        }
        RunLoop.main.add(timer!, forMode: .common)
        queueTimer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.picker.popover.isShown else { return }
                await self.picker.model.updateQueue(refreshMetadata: false)
            }
        }
        RunLoop.main.add(queueTimer!, forMode: .common)
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(refresh), name: NSWorkspace.didWakeNotification, object: nil)
        for name in [NSWorkspace.didLaunchApplicationNotification, NSWorkspace.didTerminateApplicationNotification] {
            NSWorkspace.shared.notificationCenter.addObserver(
                self, selector: #selector(applicationChanged(_:)), name: name, object: nil)
        }
        if !CommandLine.arguments.contains("--background") {
            DispatchQueue.main.async { [weak self] in self?.showPicker() }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if picker != nil { showPicker() }
        return true
    }

    private func render() {
        let now = Date()
        statusItem.button?.title = " " + (snapshot?.compactTitle(at: now) ?? "— · —")
        let usageText = snapshot?.usage?.lowestRemaining.map { "\($0)% remaining (lowest remaining window)" } ?? "Usage unavailable"
        let queueText = snapshot?.queue.map { "\($0.count) chats queued" } ?? "Queue unavailable"
        let resetText = snapshot?.usage.map { usage in
            usage.nextReset(after: now).map { "Next reset \($0.formatted(date: .abbreviated, time: .shortened))" }
                ?? "Reset due; checking automatically"
        } ?? "Reset unavailable"
        let appName = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Carrie On"
        statusItem.button?.toolTip = "\(appName) · \(queueText) · \(resetText) · \(usageText)"
        statusItem.button?.setAccessibilityLabel("\(appName), \(queueText), \(resetText)")
        writeDiagnostics()
    }

    private func writeDiagnostics() {
        guard let diagnosticsURL, let picker else { return }
        let data: [String: Any] = [
            "pid": ProcessInfo.processInfo.processIdentifier,
            "visible": statusItem.isVisible,
            "title": statusItem.button?.title ?? "",
            "presentation": "anchored-popover",
            "popover_shown": picker.popover.isShown,
            "popover_window_id": picker.popover.contentViewController?.view.window?.windowNumber ?? -1,
            "popover_frame": picker.popover.contentViewController?.view.window.map { NSStringFromRect($0.frame) } ?? "none",
            "anchor_frame": statusItem.button?.window.map { NSStringFromRect($0.frame) } ?? "none",
            "appearance": statusItem.button?.effectiveAppearance.name.rawValue ?? "unknown",
            "usage_available": snapshot?.usage != nil,
            "queue_available": snapshot?.queue != nil,
            "refreshing": refreshing,
            "connection": String(describing: picker.model.connection),
            "updated_at": picker.model.lastUpdatedAt?.timeIntervalSince1970 as Any? ?? NSNull(),
            "rendered_at": Date().timeIntervalSince1970
        ]
        if let encoded = try? JSONSerialization.data(withJSONObject: data, options: [.prettyPrinted]) {
            try? encoded.write(to: diagnosticsURL, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: diagnosticsURL.path)
        }
    }

    @objc private func refresh() {
        guard !refreshing else { return }
        refreshing = true
        Task {
            await picker.model.refreshSnapshot()
            refreshing = false
            render()
            if picker.popover.isShown { await picker.model.updateQueue() }
        }
    }

    @objc private func applicationChanged(_ notification: Notification) {
        guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
              ["com.openai.codex", "com.openai.codex.helper"].contains(app.bundleIdentifier ?? "") else { return }
        refresh()
    }

    @objc private func togglePicker() {
        if picker.popover.isShown { picker.popover.performClose(nil) }
        else { showPicker() }
    }

    private func showPicker() {
        guard let button = statusItem?.button, picker != nil else { return }
        picker.show(relativeTo: button)
        writeDiagnostics()
        refresh()
    }
}

func selfTest() throws {
    chatTitleSelfTest()
    try lifecycleSelfTest()
    let fixture = #"{"usage":{"windows":[{"bucket":"codex","name":"primary","used_percent":25,"resets_at":200,"window_minutes":300},{"bucket":"codex","name":"secondary","used_percent":91,"resets_at":500,"window_minutes":10080}]},"queue":{"count":3,"paused":false,"needs_attention":0,"service_installed":true,"last_check":100,"last_error":null},"usage_error":null,"queue_error":null}"#
    let snapshot = try MenuSnapshot.decode(Data(fixture.utf8))
    guard snapshot.compactTitle(at: Date(timeIntervalSince1970: 100)) == "3 · 2m", snapshot.queue?.count == 3,
          snapshot.usage?.lowestRemaining == 9,
          snapshot.usage?.nextReset(after: Date(timeIntervalSince1970: 100))?.timeIntervalSince1970 == 200,
          snapshot.usage?.nextReset(after: Date(timeIntervalSince1970: 300))?.timeIntervalSince1970 == 500,
          snapshot.usage?.nextReset(after: Date(timeIntervalSince1970: 600)) == nil,
          snapshot.usage?.windows.first?.label == "codex · 5-hour"
    else { fatalError("Menu summary behavior failed") }
    let unknown = try MenuSnapshot.decode(Data(#"{"usage":null,"queue":null,"usage_error":"offline","queue_error":"busy"}"#.utf8))
    guard unknown.compactTitle(at: Date()) == "— · —", unknown.queue == nil else { fatalError("Unknown data shown as available") }
    let queueOnly = try MenuSnapshot.decode(Data(#"{"queue":{"count":2,"paused":false,"needs_attention":0}}"#.utf8))
    guard queueOnly.compactTitle(at: Date()) == "2 · —" else { fatalError("Queue display requires worker-health metadata") }
    let countdown = UsageSummary(windows: [UsageWindow(bucket: "codex", name: "primary", usedPercent: 25,
                                                       resetsAt: 1_000_000, windowMinutes: 300)])
    for (remaining, expected) in [(0.0, "due"), (30, "<1m"), (60, "1m"), (61, "2m"),
                                  (3600, "1h"), (8040, "2h 14m"), (86400, "1d"), (176400, "2d 1h")] {
        guard countdown.resetCountdown(at: Date(timeIntervalSince1970: 1_000_000 - remaining)) == expected
        else { fatalError("Reset countdown failed for \(remaining) seconds") }
    }
    let invalid = fixture.replacingOccurrences(of: "\"used_percent\":25", with: "\"used_percent\":-1")
    do {
        _ = try MenuSnapshot.decode(Data(invalid.utf8))
        fatalError("Invalid usage accepted")
    } catch MenuError.invalidData {}
    MainActor.assumeIsolated {
        let recent = PickerChat(id: "recent", title: "Recent", cwd: nil, last_activity: 200)
        let older = PickerChat(id: "older", title: "Older", cwd: nil, last_activity: 100)
        let unknownActivity = PickerChat(id: "unknown", title: "Unknown", cwd: nil)
        guard [unknownActivity, older, recent].sorted(by: PickerChat.mostRecentFirst).map(\.id) == ["recent", "older", "unknown"],
              recent.activityLabel(at: Date(timeIntervalSince1970: 320)) == "2m ago",
              unknownActivity.activityDate == nil
        else { fatalError("Chat recency ordering or display failed") }
        let picker = ChatPickerModel(baseURL: URL(string: "http://127.0.0.1:1")!, didQueue: {})
        picker.connection = .connected
        picker.ready = true
        picker.chats = [.init(id: "chat-a", title: "First chat", cwd: nil),
                        .init(id: "chat-b", title: "Another chat", cwd: nil)]
        picker.reconcile(PickerQueue(entries: [.init(enrollmentID: "enrollment-a", thread_id: "chat-a", title: "First chat", prompt: "Finish the tests.")], paused: true))
        guard picker.availableChats.map(\.id) == ["chat-b"],
              picker.queued == ["chat-a"], picker.paused, picker.canQueue("chat-b"), !picker.canQueue("chat-a")
        else { fatalError("Picker queue reconciliation failed") }
        picker.instructions["chat-b"] = "Review the implementation."
        guard picker.prompt(for: "chat-b") == "Review the implementation.",
              picker.prompt(for: "chat-c") == "Please continue with your last task.",
              picker.queuedEntries.first?.prompt == "Finish the tests.",
              picker.queuedChat(picker.queuedEntries[0]).displayTitle == "First chat"
        else { fatalError("Per-chat instruction or pinned queue behavior failed") }
        picker.search = " CHAT "
        guard picker.availableChats.map(\.id) == ["chat-b"],
              picker.matchingQueuedEntries.map(\.id) == ["chat-a"]
        else { fatalError("Shared search did not match both sections") }
        picker.search = "first"
        guard picker.availableChats.isEmpty, picker.matchingQueuedEntries.map(\.id) == ["chat-a"]
        else { fatalError("Queued chat search failed") }
        picker.search = "another"
        guard picker.availableChats.map(\.id) == ["chat-b"], picker.matchingQueuedEntries.isEmpty,
              picker.queued == ["chat-a"]
        else { fatalError("Search changed queue membership or failed to filter") }
        picker.search = ""
        picker.connection = .notRunning
        picker.refreshingSummary = true
        guard !picker.canQueue("chat-b"), picker.canAct(on: "chat-a"),
              picker.queuedEntries.first?.prompt == "Finish the tests.", !picker.refreshingCodex
        else { fatalError("Offline connection blocked queue management or allowed enrollment") }
        picker.connection = .connected
        guard picker.refreshingCodex else { fatalError("Connected summary refresh is not reflected in connection activity") }
        picker.refreshingSummary = false
        guard !picker.refreshingCodex else { fatalError("Connection activity did not return to idle") }
        picker.buckets = ["first", "second"]
        guard !picker.canQueue("chat-b") else { fatalError("Ambiguous usage bucket accepted") }
        picker.bucket = "second"
        guard picker.canQueue("chat-b") else { fatalError("Explicit bucket rejected") }
        picker.activities["chat-b"] = .working("Queueing")
        guard !picker.canQueue("chat-b"), picker.canQueue("chat-c")
        else { fatalError("Card activity blocked unrelated chats or allowed duplicate submission") }
        picker.activities["chat-b"] = .uncertain("Checking")
        guard !picker.canQueue("chat-b") else { fatalError("Unconfirmed write allowed a retry") }
        var entry = picker.queuedEntries[0]
        guard entry.cardState == .queued else { fatalError("Waiting enrollment displayed as failed") }
        entry.processing_until = Date().timeIntervalSince1970 + 120
        guard entry.cardState == .queued, entry.statusDescription == "Scheduled for the next usage reset."
        else { fatalError("Readiness checking changed the queued card state") }
        entry.state = "sending"
        guard entry.cardState == .processing, entry.statusDescription == "Sending continuation…"
        else { fatalError("Dispatch has no sending indicator") }
        entry.processing_until = 1
        guard entry.cardState == .failed else { fatalError("Expired dispatch still animates") }
        entry.state = "waiting"
        entry.processing_until = nil
        entry.reactivation_error = "Codex is unavailable"
        picker.reconcile(PickerQueue(entries: [entry], paused: false))
        guard picker.queuedEntries[0].cardState == .failed,
              picker.availableChats.map(\.id) == ["chat-b"],
              entry.failureTitle == "Couldn’t resume",
              entry.failureDetail.contains("Will retry automatically.")
        else { fatalError("Failed reactivation did not stay in the queue with retry guidance") }
        entry.state = "sending"
        entry.attempted_at = 100
        guard entry.failureTitle == "Resume unconfirmed",
              !entry.failureDetail.contains("Will retry automatically.")
        else { fatalError("Uncertain delivery presented as safe to resend") }
        entry.reactivation_error = nil
        entry.state = "needs_attention"
        guard entry.cardState == .failed else { fatalError("Legacy held enrollment lost its failure state") }
        entry.attempted_at = nil
        entry.note = "Chat changed after enrollment; remove and re-enroll if still wanted"
        guard entry.failureTitle == "Needs attention", entry.failureDetail.contains("Chat changed after enrollment")
        else { fatalError("Changed chat is missing actionable feedback") }
        entry.state = "waiting"
        entry.attempted_at = nil
        guard entry.cardState == .queued else { fatalError("Recovered enrollment remained failed") }
        let draft = ChatCardContent.draft(instruction: .constant(""), canQueue: true, queue: { true })
        picker.activities["chat-b"] = .failed("Could not enqueue")
        guard draft.state == .base else { fatalError("Enqueue error became a reactivation failure") }
    }
    guard CodexConnection.resolve(installed: false, running: false, handshake: true) == .connected,
          CodexConnection.resolve(installed: false, running: false, handshake: false) == .notInstalled,
          CodexConnection.resolve(installed: true, running: false, handshake: false) == .notRunning,
          CodexConnection.resolve(installed: true, running: true, handshake: false) == .unavailable
    else { fatalError("Codex connection states were misclassified") }
    print("Menu summary checks passed")
}

if CommandLine.arguments.contains("--self-test") {
    try selfTest()
} else if let index = CommandLine.arguments.firstIndex(of: "--setup-check") {
    do {
        let result = try AppSetup.run(arguments: Array(CommandLine.arguments.dropFirst(index + 1)))
        print("Setup check passed: \(result.version), port \(result.port)")
    } catch {
        fputs("\(error.localizedDescription)\n", stderr)
        exit(1)
    }
} else {
    try AppUpdate.waitForPreviousInstance()
    let portText = Bundle.main.object(forInfoDictionaryKey: "CarrieOnPort") as? String ?? "8766"
    guard let port = Int(portText), (1024...65535).contains(port),
          let url = URL(string: "http://127.0.0.1:\(port)") else { fatalError("Invalid dashboard port") }
    MainActor.assumeIsolated {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let diagnostics = ProcessInfo.processInfo.environment["CARRIE_ON_MENU_DIAGNOSTICS"].map { URL(fileURLWithPath: $0) }
        let delegate = MenuApp(baseURL: url, diagnosticsURL: diagnostics)
        app.delegate = delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
