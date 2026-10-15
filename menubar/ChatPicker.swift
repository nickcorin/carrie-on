import AppKit
import SwiftUI

struct PickerChat: Decodable, Identifiable {
    let id: String
    let title: String?
    let cwd: String?
    var last_activity: Double? = nil

    var displayTitle: String { title ?? "Untitled chat" }
    func matches(_ query: String) -> Bool {
        let term = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return term.isEmpty || displayTitle.localizedStandardContains(term)
    }
    var location: String { cwd.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "Local chat" }
    var activityDate: Date? {
        guard let last_activity, last_activity.isFinite, last_activity > 0,
              last_activity < Date.distantFuture.timeIntervalSince1970 else { return nil }
        return Date(timeIntervalSince1970: last_activity)
    }
    func activityLabel(at now: Date) -> String {
        guard let date = activityDate else { return "Unknown activity" }
        let minutes = max(0, Int(now.timeIntervalSince(date) / 60))
        if minutes < 1 { return "Just now" }
        if minutes < 60 { return "\(minutes)m ago" }
        if minutes < 1440 { return "\(minutes / 60)h ago" }
        return "\(minutes / 1440)d ago"
    }
    static func mostRecentFirst(_ left: PickerChat, _ right: PickerChat) -> Bool {
        let leftTime = left.activityDate ?? .distantPast
        let rightTime = right.activityDate ?? .distantPast
        return leftTime == rightTime ? left.id < right.id : leftTime > rightTime
    }
}

enum ChatCardState: Equatable {
    case base, queued, processing, failed
}

struct PickerChatPage: Decodable {
    let chats: [PickerChat]
    let next_cursor: String?
    enum CodingKeys: String, CodingKey { case chats = "items", next_cursor }
}

struct PickerQueue: Decodable {
    struct Entry: Decodable, Identifiable {
        let enrollmentID: String
        let thread_id: String
        let title: String
        let prompt: String
        var state = "queued"
        var revision = 1
        var error = ""
        var dispatching = false
        var id: String { enrollmentID }
        var hasActiveDelivery: Bool { dispatching }
        var cardState: ChatCardState {
            if state == "stale" || state == "failed" { return .failed }
            if dispatching { return .processing }
            return .queued
        }
        var failureTitle: String { state == "stale" ? "Conversation continued" : "Couldn’t resume" }
        var failureDetail: String {
            if state == "stale" { return "The conversation continued after enrollment. This instruction will not be sent." }
            let reason = error.isEmpty ? "The chat could not be resumed" : error
            let sentence = reason.hasSuffix(".") ? reason : reason + "."
            return sentence + " Will retry automatically."
        }
        var statusDescription: String {
            switch cardState {
            case .failed: return "\(failureTitle). \(failureDetail)"
            case .processing: return "Sending instruction…"
            case .queued, .base: return "Scheduled for the next usage reset."
            }
        }
        var cardHeight: CGFloat { 84 }
        enum CodingKeys: String, CodingKey {
            case enrollmentID = "id"
            case thread_id = "chat_id", prompt = "instruction"
            case title, state, revision, error, dispatching
        }
        init(enrollmentID: String, thread_id: String, title: String, prompt: String) {
            self.enrollmentID = enrollmentID
            self.thread_id = thread_id
            self.title = title
            self.prompt = prompt
        }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            enrollmentID = try values.decode(String.self, forKey: .enrollmentID)
            thread_id = try values.decode(String.self, forKey: .thread_id)
            title = try values.decode(String.self, forKey: .title)
            prompt = try values.decode(String.self, forKey: .prompt)
            state = try values.decode(String.self, forKey: .state)
            revision = try values.decode(Int.self, forKey: .revision)
            error = try values.decode(String.self, forKey: .error)
            dispatching = try values.decode(Bool.self, forKey: .dispatching)
        }
    }
    let entries: [Entry]
    var next_cursor: String? = nil
    enum CodingKeys: String, CodingKey { case entries = "items", next_cursor }
}

enum CardActivity: Equatable {
    case working(String)
    case uncertain(String)
    case failed(String)
    case complete(String)

    var blocksAction: Bool {
        switch self {
        case .working, .uncertain: return true
        case .failed, .complete: return false
        }
    }
    var detail: String {
        switch self {
        case .working(let text), .uncertain(let text), .failed(let text), .complete(let text): return text
        }
    }
}

enum QueuedChange {
    case instruction(String, expected: String)
    case remove
}

/// The original creation input is retained until the server confirms its outcome.
/// Retries use these values even if the editor or selected usage bucket changes.
private struct QueueCreation: Codable, Equatable {
    let id: String
    let chatID: String
    let instruction: String
    let limitID: String?

    var body: [String: String] {
        var fields = ["id": id, "chat_id": chatID, "instruction": instruction]
        if let limitID { fields["limit_id"] = limitID }
        return fields
    }
}

private struct ServiceProblem: Decodable, LocalizedError {
    let code: String
    let detail: String
    var status = 503
    var errorDescription: String? { detail }
    enum CodingKeys: String, CodingKey { case code, detail }
}

private struct CreationJournal: Codable {
    static let key = "pendingQueueCreations.v1"
    static let maximumCount = 100
    let version: Int
    let requests: [QueueCreation]
}

private struct PendingCreation {
    enum Outcome {
        case submitting
        case uncertain(String)
        case retryable(String)
    }
    let request: QueueCreation
    var outcome: Outcome

    var activity: CardActivity {
        switch outcome {
        case .submitting: return .working("Queueing…")
        case .uncertain(let detail): return .uncertain(detail)
        case .retryable(let detail): return .failed(detail)
        }
    }
}

private enum CreationResolution {
    case confirmed(PickerQueue.Entry)
    case missing
    case unavailable(String)
}

enum RefreshTimestamp {
    static func label(_ date: Date?, at now: Date) -> String {
        guard let date else { return "Last updated: Never" }
        let age = max(0, now.timeIntervalSince(date))
        if age < 60 { return "Last updated: Just now" }
        if age < 3600 { return "Last updated: \(Int(age / 60))m ago" }
        let time = date.formatted(date: .omitted, time: .shortened)
        if Calendar.current.isDate(date, inSameDayAs: now) { return "Last updated: \(time)" }
        return "Last updated: \(date.formatted(date: .abbreviated, time: .omitted)), \(time)"
    }
}

/// Desktop availability is independent of saved queue data and usage reads.
enum CodexConnection: Equatable {
    case checking, connected, notRunning, notInstalled, unavailable, serviceUnavailable

    static func resolve(installed: Bool, running: Bool, handshake: Bool) -> Self {
        if handshake { return .connected }
        if running { return .unavailable }
        return installed ? .notRunning : .notInstalled
    }

    var title: String {
        switch self {
        case .checking: return "Connecting to Codex…"
        case .connected: return "Connected to Codex"
        case .notRunning: return "Codex isn’t open"
        case .notInstalled: return "Codex isn’t installed"
        case .unavailable: return "Waiting for Codex"
        case .serviceUnavailable: return "Reconnecting to Carrie On"
        }
    }

    var detail: String {
        switch self {
        case .checking: return "Your queue is saved on this Mac."
        case .connected: return "Ready to resume queued chats."
        case .notRunning: return "Open Codex to resume chats. Your queue is saved."
        case .notInstalled: return "Install Codex to connect. Your queue is saved."
        case .unavailable: return "Codex is open but isn’t responding yet. We’ll reconnect automatically."
        case .serviceUnavailable: return "The local service is unavailable. We’ll reconnect automatically."
        }
    }
}

@MainActor
final class ChatPickerModel: ObservableObject {
    static let defaultInstruction = "Please continue with your last task."
    @Published var search = "" {
        didSet {
            guard search != oldValue else { return }
            searchRevision += 1
            nextCursor = nil
            searching = false
            loadError = nil
        }
    }
    @Published var instructions: [String: String] = [:]
    @Published var bucket = ""
    @Published var chats: [PickerChat] = []
    @Published var queuedEntries: [PickerQueue.Entry] = []
    @Published var buckets: [String] = []
    @Published var activities: [String: CardActivity] = [:]
    @Published var searching = false
    @Published private(set) var nextCursor: String?
    @Published var checkingQueue = false
    @Published var loadError: String?
    @Published var ready = false
    @Published var connection: CodexConnection = .checking
    @Published private(set) var lastUpdatedAt: Date?
    @Published private(set) var openingCodex = false
    @Published private(set) var launchError: String?
    @Published private(set) var queueRecoveryError: String?
    @Published var refreshingSummary = false
    @Published var summary: MenuSnapshot? {
        didSet {
            guard let usage = summary?.usage else { return }
            buckets = Array(Set(usage.windows.map(\.bucket))).sorted()
            if !buckets.contains(bucket) { bucket = buckets.count == 1 ? buckets[0] : "" }
        }
    }
    @Published private var knownChats: [String: PickerChat] = [:]
    @Published private var checkingMetadata = false
    private var searchRevision = 0
    private var catalogQuery: String?
    private var queueRevision = 0
    private var snapshotRefresh: Task<Void, Never>?
    @Published private var pendingCreations: [String: PendingCreation] = [:]
    private let recoveryDefaults: UserDefaults?
    private let baseURL: URL
    private let session: URLSession
    private let didQueue: () -> Void

    /// Owns requests in the app's profile-specific preferences domain until their outcomes are known.
    /// Pass nil preferences only for isolated previews or tests that do not need restart recovery.
    /// An unreadable journal blocks new creation while existing queue actions remain available.
    init(baseURL: URL, session: URLSession = .shared, recoveryDefaults: UserDefaults? = .standard,
         didQueue: @escaping () -> Void) {
        self.baseURL = baseURL
        self.session = session
        self.recoveryDefaults = recoveryDefaults
        self.didQueue = didQueue
        guard let data = recoveryDefaults?.data(forKey: CreationJournal.key) else { return }
        guard data.count <= 16_000_000,
              let saved = try? JSONDecoder().decode(CreationJournal.self, from: data),
              saved.version == 1, saved.requests.count <= CreationJournal.maximumCount,
              Set(saved.requests.map(\.chatID)).count == saved.requests.count,
              saved.requests.allSatisfy({ UUID(uuidString: $0.id) != nil && UUID(uuidString: $0.chatID) != nil
                  && !$0.instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                  && $0.instruction.count <= 32000 && ($0.limitID?.count ?? 0) <= 200 }) else {
            queueRecoveryError = "Saved queue requests could not be read. Existing items remain available; new requests are blocked to prevent duplicate sends."
            return
        }
        for request in saved.requests {
            pendingCreations[request.chatID] = PendingCreation(request: request,
                outcome: .uncertain("Checking the previous queue request…"))
        }
    }

    var queued: Set<String> { Set(queuedEntries.map(\.thread_id)) }
    var availableChats: [PickerChat] {
        chats.filter { !queued.contains($0.id) && $0.matches(search) }.sorted(by: PickerChat.mostRecentFirst)
    }
    var matchingQueuedEntries: [PickerQueue.Entry] {
        queuedEntries.filter { queuedChat($0).matches(search) }
            .sorted { PickerChat.mostRecentFirst(queuedChat($0), queuedChat($1)) }
    }
    var working: Bool {
        (Array(activities.values) + pendingCreations.values.map(\.activity))
            .contains { if case .working = $0 { return true }; return false }
    }
    var refreshingCodex: Bool {
        connection == .connected && (refreshingSummary || searching || checkingMetadata)
    }
    func queuedChat(_ entry: PickerQueue.Entry) -> PickerChat {
        knownChats[entry.thread_id] ?? PickerChat(id: entry.thread_id, title: entry.title, cwd: nil)
    }
    func prompt(for id: String) -> String {
        let text = (instructions[id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? Self.defaultInstruction : text
    }
    func activity(for id: String) -> CardActivity? { pendingCreations[id]?.activity ?? activities[id] }
    func canAct(on id: String) -> Bool { activity(for: id)?.blocksAction != true }
    func canQueue(_ id: String) -> Bool {
        connection == .connected && queueRecoveryError == nil && canAct(on: id) && !queued.contains(id)
            && (pendingCreations[id] != nil || buckets.count <= 1 || !bucket.isEmpty)
    }

    private func request(_ path: String, query: String? = nil, cursor: String? = nil, body: [String: String]? = nil,
                         method: String = "POST", revision: Int? = nil, timeout: Double = 120) async throws -> Data {
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if let query { components.queryItems = [URLQueryItem(name: "search", value: query)] }
        if let cursor { components.queryItems = (components.queryItems ?? []) + [URLQueryItem(name: "cursor", value: cursor)] }
        var request = URLRequest(url: components.url!)
        request.setValue("1", forHTTPHeaderField: "X-Carrie-On")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = timeout
        if let body {
            request.httpMethod = method
            request.setValue(method == "PATCH" ? "application/merge-patch+json" : "application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
        }
        if let revision { request.setValue("\"revision-\(revision)\"", forHTTPHeaderField: "If-Match") }
        let (data, response) = try await session.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 503
        guard (200..<300).contains(status) else {
            var problem = (try? JSONDecoder().decode(ServiceProblem.self, from: data))
                ?? ServiceProblem(code: "invalid_response", detail: "Local service unavailable. Try again shortly.")
            problem.status = status
            throw problem
        }
        return data
    }

    func load() async {
        async let queue: () = updateQueue()
        await refreshSnapshot()
        async let catalog: () = searchChats()
        async let metadata: () = loadQueuedMetadata()
        _ = await (catalog, queue, metadata)
    }

    private var codexApplicationURL: URL? {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.openai.codex"),
              FileManager.default.fileExists(atPath: url.path) else { return nil }
        return url
    }

    func openCodex() async {
        guard connection == .notRunning, !openingCodex else { return }
        guard let url = codexApplicationURL else {
            connection = .notInstalled
            return
        }
        openingCodex = true
        launchError = nil
        defer { openingCodex = false }
        do {
            _ = try await NSWorkspace.shared.openApplication(at: url, configuration: .init())
            await refreshSnapshot()
        } catch {
            launchError = error.localizedDescription
        }
    }

    /// Refresh connection, usage, and queue together; overlapping callers share one poll.
    /// Partial or failed reads preserve the last complete update time.
    func refreshSnapshot() async {
        if let snapshotRefresh {
            await snapshotRefresh.value
            return
        }
        let refresh = Task { await pollSnapshot() }
        snapshotRefresh = refresh
        await refresh.value
        snapshotRefresh = nil
    }

    private func pollSnapshot() async {
        refreshingSummary = true
        defer { refreshingSummary = false }
        let previous = connection
        let identifiers = ["com.openai.codex", "com.openai.codex.helper"]
        let running = NSWorkspace.shared.runningApplications.contains { identifiers.contains($0.bundleIdentifier ?? "") }
        let installed = running || codexApplicationURL != nil
        if !running { connection = CodexConnection.resolve(installed: installed, running: false, handshake: false) }
        do {
            let data = try await request("api/v1/status", timeout: 45)
            let snapshot = try MenuSnapshot.decode(data)
            guard let handshake = snapshot.connected else { throw MenuError.invalidData }
            summary = snapshot
            connection = CodexConnection.resolve(installed: installed, running: running, handshake: handshake)
            if snapshot.usage != nil, snapshot.queue != nil { lastUpdatedAt = Date() }
        } catch {
            summary = nil
            connection = running ? .serviceUnavailable : CodexConnection.resolve(installed: installed, running: false, handshake: false)
        }
        if connection != .connected { loadError = nil }
        if connection != .notRunning { launchError = nil }
        if connection == .connected, previous != .connected {
            Task { await searchChats(); await loadQueuedMetadata() }
        }
    }

    private func loadQueuedMetadata() async {
        guard connection == .connected, !checkingMetadata else { return }
        checkingMetadata = true
        defer { checkingMetadata = false }
        do {
            var cursor: String? = nil
            repeat {
                let data = try await request("api/v1/queue/metadata", cursor: cursor)
                let page = try JSONDecoder().decode(PickerChatPage.self, from: data)
                for chat in page.chats {
                    if chat.activityDate != nil || knownChats[chat.id] == nil { knownChats[chat.id] = chat }
                }
                cursor = page.next_cursor
            } while cursor != nil
        } catch { /* Missing metadata is shown as unknown; cached metadata remains available. */ }
    }

    func searchChats() async {
        guard connection == .connected, !searching else { return }
        searchRevision += 1
        nextCursor = nil
        catalogQuery = search.trimmingCharacters(in: .whitespacesAndNewlines)
        await loadChatPage(cursor: nil)
    }

    func loadMoreChats() async {
        guard connection == .connected, !searching, let nextCursor,
              catalogQuery == search.trimmingCharacters(in: .whitespacesAndNewlines) else { return }
        await loadChatPage(cursor: nextCursor)
    }

    private func loadChatPage(cursor: String?) async {
        let revision = searchRevision
        let query = search.trimmingCharacters(in: .whitespacesAndNewlines)
        searching = true
        loadError = nil
        defer { if revision == searchRevision { searching = false } }
        do {
            let data = try await request("api/v1/chats", query: query, cursor: cursor)
            let page = try JSONDecoder().decode(PickerChatPage.self, from: data)
            guard revision == searchRevision, !Task.isCancelled else { return }
            var ids = Set<String>()
            chats = ((cursor == nil ? [] : chats) + page.chats).filter { ids.insert($0.id).inserted }
            for chat in page.chats { knownChats[chat.id] = chat }
            nextCursor = page.next_cursor
            ready = true
            loadError = nil
        } catch {
            guard !Task.isCancelled else { return }
            if revision == searchRevision, connection == .connected {
                loadError = "Could not load chats: \(error.localizedDescription)"
                await refreshSnapshot()
            }
        }
    }

    func updateQueue(refreshMetadata: Bool = true) async {
        guard !checkingQueue, !working else { return }
        if !ready && !searching { Task { await searchChats() } }
        let mutationRevision = queueRevision
        checkingQueue = true
        defer { checkingQueue = false }
        do {
            var cursor: String? = nil
            var entries: [PickerQueue.Entry] = []
            var seen = Set<String>()
            repeat {
                let data = try await request("api/v1/queue", cursor: cursor)
                let page = try JSONDecoder().decode(PickerQueue.self, from: data)
                entries += page.entries.filter { seen.insert($0.enrollmentID).inserted }
                cursor = page.next_cursor
            } while cursor != nil
            var resolutions: [String: CreationResolution] = [:]
            for (chatID, pending) in pendingCreations {
                if case .submitting = pending.outcome { continue }
                let itemID = pending.request.id
                if let item = entries.first(where: { $0.enrollmentID == itemID && $0.thread_id == chatID }) {
                    resolutions[chatID] = .confirmed(item)
                    continue
                }
                do {
                    let data = try await request("api/v1/queue/\(itemID)")
                    let item = try JSONDecoder().decode(PickerQueue.Entry.self, from: data)
                    guard item.enrollmentID == itemID, item.thread_id == chatID else {
                        resolutions[chatID] = .unavailable("The saved request conflicts with another queue item. Its outcome is still unknown.")
                        continue
                    }
                    resolutions[chatID] = .confirmed(item)
                } catch let problem as ServiceProblem where problem.status == 404 && problem.code == "not_found" {
                    resolutions[chatID] = .missing
                } catch {
                    resolutions[chatID] = .unavailable(error.localizedDescription)
                }
            }
            // A read started before a mutation must not undo the mutation's result.
            guard mutationRevision == queueRevision, !working else { return }
            let previousPendingCount = pendingCreations.count
            for (chatID, resolution) in resolutions {
                switch resolution {
                case .confirmed(let item):
                    pendingCreations.removeValue(forKey: chatID)
                    activities[chatID] = .complete(item.state == "queued" ? "Queued" : "Previous request: \(item.state).")
                    if ["queued", "stale", "failed"].contains(item.state), seen.insert(item.enrollmentID).inserted {
                        entries.append(item)
                    }
                case .missing:
                    pendingCreations[chatID]?.outcome = .retryable("The request was not found. Retry will use its saved instruction and usage bucket.")
                case .unavailable(let reason):
                    pendingCreations[chatID]?.outcome = .uncertain(reason + " Checking the previous request before allowing another send.")
                }
            }
            if pendingCreations.count != previousPendingCount { try savePendingCreations() }
            queuedEntries = entries
            if refreshMetadata { Task { await loadQueuedMetadata() } }
            for (id, activity) in activities where pendingCreations[id] == nil {
                if case .uncertain(let reason) = activity {
                    activities[id] = .failed(reason + " Queue status has been refreshed.")
                }
            }
        } catch { /* Existing cards stay usable; unknown writes remain blocked on their own card. */ }
    }

    private func savePendingCreations() throws {
        guard let recoveryDefaults else { return }
        let saved = CreationJournal(version: 1, requests: pendingCreations.values.map(\.request))
        recoveryDefaults.set(try JSONEncoder().encode(saved), forKey: CreationJournal.key)
    }

    private func begin(_ id: String, detail: String) {
        queueRevision += 1
        activities[id] = .working(detail)
    }

    private func finish(_ id: String, result: CardActivity) {
        queueRevision += 1
        activities[id] = result
        didQueue()
        Task { await updateQueue() }
    }

    func queueChat(_ chat: PickerChat) async -> Bool {
        guard canQueue(chat.id) else { return false }
        let prompt = prompt(for: chat.id)
        guard pendingCreations[chat.id] != nil || prompt.count <= 32000 else {
            activities[chat.id] = .failed("Keep the instruction under 32,000 characters.")
            return false
        }
        guard pendingCreations[chat.id] != nil || pendingCreations.count < CreationJournal.maximumCount else {
            activities[chat.id] = .failed("Resolve previous queue requests before creating more.")
            return false
        }
        let creation = pendingCreations[chat.id]?.request ?? QueueCreation(id: UUID().uuidString.lowercased(),
            chatID: chat.id, instruction: prompt, limitID: bucket.isEmpty ? nil : bucket)
        queueRevision += 1
        pendingCreations[chat.id] = PendingCreation(request: creation, outcome: .submitting)
        do {
            try savePendingCreations()
            let data = try await request("api/v1/queue", body: creation.body)
            let entry = try JSONDecoder().decode(PickerQueue.Entry.self, from: data)
            guard entry.enrollmentID == creation.id, entry.thread_id == creation.chatID else { throw MenuError.invalidData }
            pendingCreations.removeValue(forKey: chat.id)
            try savePendingCreations()
            queuedEntries.removeAll { $0.enrollmentID == entry.enrollmentID }
            if ["queued", "stale", "failed"].contains(entry.state) { queuedEntries.append(entry) }
            finish(chat.id, result: .complete(entry.state == "queued" ? "Queued" : "Previous request: \(entry.state)."))
            return true
        } catch {
            if let problem = error as? ServiceProblem, (400..<500).contains(problem.status),
               problem.code != "id_reused", problem.code != "invalid_response", problem.status != 408 {
                pendingCreations.removeValue(forKey: chat.id)
                try? savePendingCreations()
                finish(chat.id, result: .failed(problem.detail))
            } else {
                pendingCreations[chat.id]?.outcome = .uncertain(error.localizedDescription)
                queueRevision += 1
                didQueue()
                Task { await updateQueue() }
            }
            return false
        }
    }

    func changeQueued(_ entry: PickerQueue.Entry, change: QueuedChange) async -> Bool {
        guard canAct(on: entry.thread_id), !entry.hasActiveDelivery else { return false }
        begin(entry.thread_id, detail: "Updating…")
        do {
            switch change {
            case .instruction(let text, let expected):
                guard entry.prompt == expected else { throw MenuError.invalidData }
                let prompt = text.trimmingCharacters(in: .whitespacesAndNewlines)
                let data = try await request("api/v1/queue/\(entry.enrollmentID)", body: [
                    "instruction": prompt.isEmpty ? Self.defaultInstruction : prompt
                ], method: "PATCH", revision: entry.revision)
                let saved = try JSONDecoder().decode(PickerQueue.Entry.self, from: data)
                if let index = queuedEntries.firstIndex(where: { $0.enrollmentID == entry.enrollmentID }) {
                    queuedEntries[index] = saved
                }
            case .remove:
                _ = try await request("api/v1/queue/\(entry.enrollmentID)/cancel", body: [:])
                instructions[entry.thread_id] = entry.prompt
                queuedEntries.removeAll { $0.enrollmentID == entry.enrollmentID }
            }
            finish(entry.thread_id, result: .complete("Updated"))
            return true
        } catch {
            finish(entry.thread_id, result: .uncertain(error.localizedDescription))
            return false
        }
    }
}

struct SectionDisclosureStyle: DisclosureGroupStyle {
    func makeBody(configuration: Configuration) -> some View {
        VStack(spacing: 0) {
            Button { configuration.isExpanded.toggle() } label: {
                HStack(spacing: 8) {
                    Image(systemName: configuration.isExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                        .frame(width: 10)
                    configuration.label.fixedSize()
                    Rectangle().fill(Color(nsColor: .separatorColor)).frame(height: 1)
                }
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 28)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(configuration.isExpanded ? "Expanded" : "Collapsed")
            if configuration.isExpanded { configuration.content }
        }
    }
}

struct ChatPickerView: View {
    @ObservedObject var model: ChatPickerModel
    @ObservedObject var lifecycle: AppLifecycle
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var expandedQueued: Set<String> = []
    @State private var hoveringQueued = false
    @State private var hoveringChats = false
    @AppStorage("queuedSectionExpanded") private var showQueued = true
    @AppStorage("chatsSectionExpanded") private var showChats = true

    private var cardTransition: AnyTransition {
        reduceMotion ? .identity : .opacity.combined(with: .scale(scale: 0.96))
    }

    private var queuedHeight: CGFloat {
        min(200, model.matchingQueuedEntries.reduce(CGFloat(0)) { height, entry in
            height + (expandedQueued.contains(entry.enrollmentID) ? 122 : entry.cardHeight + 6)
        })
    }

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 10) {
                    Image(nsImage: Brand.trayIcon).renderingMode(.template).resizable()
                        .frame(width: 22, height: 22).foregroundStyle(Color.accentColor)
                        .accessibilityHidden(true)
                    Text(Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Carrie On")
                        .font(.system(size: 18, weight: .semibold))
                    Spacer()
                    Menu {
                        Text("Version \(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Unknown")")
                        Divider()
                        Toggle(isOn: Binding(
                            get: { lifecycle.launchAtLogin },
                            set: { lifecycle.setLaunchAtLogin($0) })) {
                            Label("Launch at Login", systemImage: "power")
                        }
                            .disabled(lifecycle.busy || !lifecycle.ready)
                        Button { lifecycle.checkForUpdates() } label: {
                            Label(lifecycle.updateActionTitle, systemImage: "arrow.triangle.2.circlepath")
                        }
                            .disabled(lifecycle.busy || !lifecycle.ready)
                        Divider()
                        Button { lifecycle.quit() } label: {
                            Label("Quit", systemImage: "power.circle")
                        }
                            .disabled(lifecycle.busy)
                    } label: {
                        Image(systemName: "gearshape")
                            .font(.system(size: 15, weight: .medium))
                            .frame(width: 28, height: 28)
                            .contentShape(Rectangle())
                    }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden)
                    .accessibilityLabel("Carrie On menu")
                    .help("Carrie On menu")
                }
                if model.connection == .connected { usageSummary }
            }.padding(20)

            HStack(spacing: 9) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Search", text: $model.search)
                    .textFieldStyle(.plain)
                    .onSubmit { Task { await model.searchChats() } }
                if !model.search.isEmpty {
                    Button { model.search = "" } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                    }
                    .buttonStyle(.borderless).accessibilityLabel("Clear search")
                }
            }
            .padding(9)
            .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 9))
            .padding(.horizontal, 20).padding(.bottom, 14)
            .task(id: model.search) {
                do { try await Task.sleep(nanoseconds: 250_000_000) } catch { return }
                await model.searchChats()
            }

            DisclosureGroup(isExpanded: $showQueued) {
                if model.matchingQueuedEntries.isEmpty {
                    sectionMessage(model.search.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        ? "No queued chats" : "No matching chats")
                        .padding(.top, 8)
                } else {
                    List {
                        Group {
                            ForEach(model.matchingQueuedEntries) { entry in
                                ChatCard(chat: model.queuedChat(entry), activity: model.activity(for: entry.thread_id),
                                    content: .queued(entry,
                                        save: { text, expected in
                                            await model.changeQueued(entry, change: .instruction(text, expected: expected))
                                        }, remove: { await model.changeQueued(entry, change: .remove) }),
                                    onFlip: { flipped in
                                        if flipped { expandedQueued.insert(entry.enrollmentID) }
                                        else { expandedQueued.remove(entry.enrollmentID) }
                                    })
                                    .transition(cardTransition)
                                    .padding(.bottom, 6)
                                    .listRowInsets(EdgeInsets())
                            }
                        }.listRowInsets(EdgeInsets())
                            .listRowSeparator(.hidden)
                            .listRowBackground(Color.clear)
                            .background(ChatListAppearance(hovered: hoveringQueued))
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                    .frame(height: queuedHeight)
                    .onHover { hoveringQueued = $0 }
                    .padding(.top, 6)
                    .accessibilityLabel("Queued chats")
                }
            } label: {
                Label("Queued · \(model.matchingQueuedEntries.count)", systemImage: "hourglass")
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            }.padding(.horizontal, 20).padding(.bottom, 12)

            DisclosureGroup(isExpanded: $showChats) {
                VStack(spacing: 8) {
                    if let error = model.queueRecoveryError {
                        Text(error).font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if model.buckets.count > 1 {
                        Picker("Usage bucket", selection: $model.bucket) {
                            Text("Select a bucket").tag("")
                            ForEach(model.buckets, id: \.self) { Text($0).tag($0) }
                        }
                    }

                    List {
                        Group {
                            ForEach(model.availableChats) { chat in
                                ChatCard(chat: chat, activity: model.activity(for: chat.id), content: .draft(
                                    instruction: Binding(
                                        get: { model.instructions[chat.id] ?? "" },
                                        set: { model.instructions[chat.id] = $0 }),
                                    canQueue: model.canQueue(chat.id),
                                    queue: { await model.queueChat(chat) }), onFlip: { _ in })
                                    .transition(cardTransition)
                                    .padding(.bottom, 6)
                                    .listRowInsets(EdgeInsets())
                            }
                            if let cursor = model.nextCursor {
                                VStack(spacing: 6) {
                                    if model.searching {
                                        chatPlaceholders
                                    } else {
                                        Button(model.loadError == nil ? "Load more chats" : "Retry loading chats") {
                                            Task { await model.loadMoreChats() }
                                        }.buttonStyle(.borderless)
                                    }
                                }
                                .frame(maxWidth: .infinity).padding(.vertical, 8)
                                .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: model.searching)
                                .id(cursor)
                                .task {
                                    if model.loadError == nil { await model.loadMoreChats() }
                                }
                            } else if model.loadError != nil {
                                Button("Retry loading chats") { Task { await model.searchChats() } }
                                    .buttonStyle(.borderless).padding(.vertical, 8)
                            }
                            if model.availableChats.isEmpty && model.nextCursor == nil && model.loadError == nil {
                                sectionMessage(model.connection != .connected ? "Chats will appear when connected" :
                                    (model.searching ? "Finding chats…" : (model.ready ? "No matching chats" : "Loading chats…")))
                            }
                        }.listRowInsets(EdgeInsets())
                            .listRowSeparator(.hidden)
                            .listRowBackground(Color.clear)
                            .background(ChatListAppearance(hovered: hoveringChats))
                    }.listStyle(.plain)
                        .scrollContentBackground(.hidden)
                        .onHover { hoveringChats = $0 }
                        .accessibilityLabel("Available chats")
                }.padding(.top, 8)
            } label: {
                Label("Chats · \(model.availableChats.count)\(model.nextCursor == nil ? "" : "+")", systemImage: "bubble.left.and.bubble.right")
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 20)
            if !showChats { Spacer(minLength: 0) }
            Divider().padding(.horizontal, 20)
            connectionFooter
        }
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.3),
                   value: model.queuedEntries.map(\.enrollmentID).sorted())
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.3),
                   value: model.queuedEntries.map(\.cardState))
        .disclosureGroupStyle(SectionDisclosureStyle())
        .frame(width: 440, height: 620, alignment: .top)
    }

    private var chatPlaceholders: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(0..<2) { _ in
                VStack(alignment: .leading, spacing: 8) {
                    RoundedRectangle(cornerRadius: 3).frame(width: 210, height: 9)
                    RoundedRectangle(cornerRadius: 3).frame(width: 125, height: 7)
                }
                .foregroundStyle(Color.secondary.opacity(0.12))
                .frame(maxWidth: .infinity, alignment: .leading).padding(14)
                .background(Color.primary.opacity(0.025), in: RoundedRectangle(cornerRadius: 10))
            }
        }
        .accessibilityHidden(true)
        .transition(.opacity)
    }

    private func sectionMessage(_ message: String) -> some View {
        Text(message)
            .font(.caption).foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity, alignment: .center)
            .padding(.vertical, 8)
    }

    private var connectionError: String? {
        model.launchError ?? (model.connection == .connected ? model.loadError : nil)
    }

    private var connectionLabel: String {
        if model.launchError != nil { return "Couldn’t open Codex" }
        if connectionError != nil { return "Connected · couldn’t refresh chats" }
        return model.connection.title
    }

    private var connectionFooter: some View {
        HStack(spacing: 8) {
            HStack(spacing: 7) {
                ZStack {
                    if model.refreshingCodex || model.connection == .checking || model.openingCodex {
                        ProgressView().controlSize(.mini)
                    } else if connectionError != nil {
                        Image(systemName: "exclamationmark.circle").font(.system(size: 11)).foregroundStyle(.orange)
                    } else {
                        Circle()
                            .fill(model.connection == .connected ? Color.green : Color.secondary.opacity(0.5))
                            .frame(width: 6, height: 6)
                    }
                }
                .frame(width: 14, height: 14)
                .accessibilityHidden(true)
                Text(connectionLabel)
                    .font(.system(size: 11)).lineLimit(1)
            }
            .accessibilityElement(children: .combine)
            Spacer(minLength: 0)
            TimelineView(.periodic(from: .now, by: 30)) { context in
                Text(RefreshTimestamp.label(model.lastUpdatedAt, at: context.date))
                    .font(.system(size: 11))
                    .monospacedDigit()
                    .lineLimit(1)
            }
            if model.connection == .notRunning {
                Button {
                    Task { await model.openCodex() }
                } label: {
                    Text(model.openingCodex ? "Opening…" : "Open Codex")
                        .font(.system(size: 11, weight: .medium))
                        .padding(.horizontal, 9).padding(.vertical, 5)
                }
                .buttonStyle(CardActionStyle())
                .disabled(model.openingCodex)
                .accessibilityLabel("Open Codex")
            }
        }
        .foregroundStyle(.secondary)
        .accessibilityElement(children: .contain)
        .frame(minHeight: 26)
        .padding(.horizontal, 20).padding(.vertical, 8)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func usageColor(for remaining: Int) -> Color {
        let progress = CGFloat(min(100, max(0, remaining))) / 100
        let start: NSColor = progress < 0.5 ? .systemRed : .systemOrange
        let end: NSColor = progress < 0.5 ? .systemOrange : .systemGreen
        let fraction = progress < 0.5 ? progress * 2 : (progress - 0.5) * 2
        return Color(nsColor: start.blended(withFraction: fraction, of: end) ?? start)
    }

    private var usageSummary: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("Usage remaining")
                Spacer()
                TimelineView(.periodic(from: .now, by: 30)) { context in
                    if let usage = model.summary?.usage {
                        Label(usage.nextReset(after: context.date) == nil ? "Reset due" : "Resets in \(usage.resetCountdown(at: context.date))",
                              systemImage: "clock")
                            .monospacedDigit()
                    }
                }
            }.font(.caption).foregroundStyle(.secondary)
            if let remaining = model.summary?.usage?.lowestRemaining {
                GeometryReader { geometry in
                    ZStack {
                        Capsule().fill(usageColor(for: remaining).opacity(0.1))
                        HStack(spacing: 0) {
                            Rectangle().fill(usageColor(for: remaining).opacity(0.5))
                                .frame(width: geometry.size.width * Double(remaining) / 100)
                            Spacer(minLength: 0)
                        }.clipShape(Capsule())
                        Text("\(remaining)%").font(.system(size: 11, weight: .semibold)).monospacedDigit()
                    }
                }
                .frame(height: 22)
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.5), value: remaining)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Usage remaining")
                .accessibilityValue("\(remaining) percent remaining")
            } else {
                Text("Usage unavailable").font(.caption).foregroundStyle(.secondary)
            }
        }
        .help(model.summary?.usage?.windows.map {
            "\($0.label): \($0.remainingPercent)% remaining · resets \(Date(timeIntervalSince1970: $0.resetsAt).formatted())"
        }.joined(separator: "\n") ?? "Updates automatically every minute")
    }
}

enum ChatCardContent {
    case draft(instruction: Binding<String>, canQueue: Bool, queue: () async -> Bool)
    case queued(PickerQueue.Entry, save: (String, String) async -> Bool, remove: () async -> Bool)

    var state: ChatCardState {
        switch self {
        case .draft: return .base
        case .queued(let entry, _, _): return entry.cardState
        }
    }
}

struct InstructionEditor: NSViewRepresentable {
    @Binding var text: String
    let focused: Bool
    let enabled: Bool
    let label: String
    let submit: () -> Void
    let cancel: () -> Void

    final class TextView: NSTextView {
        var submit: () -> Void = {}
        var cancel: () -> Void = {}
        override func keyDown(with event: NSEvent) {
            // A menu-bar app has no Edit menu to route standard text shortcuts.
            if event.modifierFlags.contains(.command) {
                switch event.charactersIgnoringModifiers?.lowercased() {
                case "a": selectAll(nil); return
                case "c": copy(nil); return
                case "x": if isEditable { cut(nil) }; return
                case "v": if isEditable { paste(nil) }; return
                case "z":
                    if isEditable {
                        if event.modifierFlags.contains(.shift) { undoManager?.redo() }
                        else { undoManager?.undo() }
                    }
                    return
                default: break
                }
            }
            if hasMarkedText() { super.keyDown(with: event) }
            else if event.keyCode == 36 || event.keyCode == 76 {
                if event.modifierFlags.contains(.shift) { insertNewline(nil) }
                else { submit() }
            } else if event.keyCode == 53 { cancel() }
            else { super.keyDown(with: event) }
        }
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var editor: InstructionEditor
        var focused = false
        init(_ editor: InstructionEditor) { self.editor = editor }
        func textDidChange(_ notification: Notification) {
            guard let view = notification.object as? NSTextView else { return }
            editor.text = view.string
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        let view = TextView()
        view.isRichText = false
        view.allowsUndo = true
        view.drawsBackground = false
        view.font = .systemFont(ofSize: 12)
        view.textColor = .labelColor
        view.textContainerInset = NSSize(width: 0, height: 2)
        view.textContainer?.lineFragmentPadding = 0
        view.isHorizontallyResizable = false
        view.isVerticallyResizable = true
        view.autoresizingMask = [.width]
        view.textContainer?.widthTracksTextView = true
        view.delegate = context.coordinator
        scroll.documentView = view
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let view = scroll.documentView as? TextView else { return }
        context.coordinator.editor = self
        if view.string != text { view.string = text }
        view.isEditable = enabled
        view.setAccessibilityLabel(label)
        view.submit = submit
        view.cancel = cancel
        if focused && !context.coordinator.focused {
            DispatchQueue.main.async { [weak view] in
                guard let view, context.coordinator.editor.focused else { return }
                view.window?.makeFirstResponder(view)
                view.setSelectedRange(NSRange(location: view.string.utf16.count, length: 0))
            }
        } else if !focused && context.coordinator.focused, view.window?.firstResponder === view {
            view.window?.makeFirstResponder(nil)
        }
        context.coordinator.focused = focused
    }
}

struct CardActionStyle: ButtonStyle {
    var destructive = false

    func makeBody(configuration: Configuration) -> some View {
        Feedback(configuration: configuration, destructive: destructive)
    }

    private struct Feedback: View {
        let configuration: ButtonStyle.Configuration
        let destructive: Bool
        @State private var hovered = false
        @Environment(\.isEnabled) private var enabled
        @Environment(\.accessibilityReduceMotion) private var reduceMotion

        private var tint: Color { destructive ? .red : .primary }

        var body: some View {
            configuration.label
                .foregroundStyle(tint)
                .background(tint.opacity(enabled && configuration.isPressed ? 0.22 : (enabled && hovered ? 0.14 : 0.04)),
                            in: RoundedRectangle(cornerRadius: 7))
                .overlay(RoundedRectangle(cornerRadius: 7)
                    .strokeBorder(tint.opacity(enabled && hovered ? 0.22 : 0)))
                .contentShape(RoundedRectangle(cornerRadius: 7))
                .opacity(enabled ? 1 : 0.4)
                .onHover { hovered = $0 }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.12), value: hovered)
                .animation(reduceMotion ? nil : .easeOut(duration: 0.08), value: configuration.isPressed)
        }
    }
}

/// Align native list cells with the cards and keep scrolling controls unobtrusive.
struct ChatListAppearance: NSViewRepresentable {
    let hovered: Bool

    final class Scroller: NSScroller {
        var hovered = false { didSet { needsDisplay = true } }
        private var scrollDeadline = Date.distantPast
        private var hideTimer: Timer?

        override var doubleValue: Double {
            didSet {
                guard doubleValue != oldValue else { return }
                scrollDeadline = Date().addingTimeInterval(0.8)
                needsDisplay = true
                hideTimer?.invalidate()
                let timer = Timer(timeInterval: 0.8, repeats: false) { [weak self] _ in
                    self?.needsDisplay = true
                }
                hideTimer = timer
                RunLoop.main.add(timer, forMode: .common)
            }
        }

        var showsThumb: Bool {
            knobProportion < 1 && isEnabled && (hovered || Date() < scrollDeadline)
        }

        override func drawKnobSlot(in slotRect: NSRect, highlight flag: Bool) {}

        override func drawKnob() {
            guard showsThumb else { return }
            let knob = rect(for: .knob)
            let thumb = NSRect(x: knob.midX - 2, y: knob.minY + 2,
                               width: 4, height: max(0, knob.height - 4))
            NSColor.labelColor.withAlphaComponent(0.24).setFill()
            NSBezierPath(roundedRect: thumb, xRadius: 2, yRadius: 2).fill()
        }
    }

    final class View: NSView {
        var hovered = false
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            configureScrollView()
        }

        func configureScrollView() {
            guard let scroll = enclosingScrollView else { return }
            if let table = scroll.documentView as? NSTableView, table.intercellSpacing.width != 0 {
                table.intercellSpacing.width = 0
            }
            if !(scroll.verticalScroller is Scroller) {
                scroll.verticalScroller = Scroller()
            }
            // Own visibility so the list hover remains visible for as long as needed.
            scroll.scrollerStyle = .legacy
            scroll.autohidesScrollers = true
            scroll.verticalScroller?.controlSize = .small
            (scroll.verticalScroller as? Scroller)?.hovered = hovered
        }
    }

    func makeNSView(context: Context) -> View { View() }

    func updateNSView(_ view: View, context: Context) {
        view.hovered = hovered
        view.configureScrollView()
    }
}

/// Show hover help only when the available width cannot hold the complete title.
struct ChatCardTitle: NSViewRepresentable {
    let title: String
    var size: CGFloat = 13

    final class Label: NSTextField {
        override var intrinsicContentSize: NSSize {
            let lineFont = font ?? NSFont.systemFont(ofSize: 13)
            return NSSize(width: NSView.noIntrinsicMetric,
                          height: ceil(lineFont.ascender - lineFont.descender + lineFont.leading))
        }

        override func layout() {
            super.layout()
            let fullWidth = (stringValue as NSString).size(withAttributes: [
                .font: font ?? NSFont.systemFont(ofSize: 13)
            ]).width
            let availableWidth = cell?.drawingRect(forBounds: bounds).width ?? bounds.width
            toolTip = fullWidth > availableWidth || stringValue.contains("\n") ? stringValue : nil
        }
    }

    func makeNSView(context: Context) -> Label {
        let label = Label(labelWithString: title)
        label.maximumNumberOfLines = 1
        label.cell?.usesSingleLineMode = true
        label.lineBreakMode = .byTruncatingTail
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return label
    }

    func updateNSView(_ label: Label, context: Context) {
        label.stringValue = title
        label.font = .systemFont(ofSize: size, weight: .semibold)
        label.needsLayout = true
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: Label, context: Context) -> CGSize? {
        CGSize(width: proposal.width ?? 0, height: nsView.intrinsicContentSize.height)
    }
}

func chatTitleSelfTest() {
    let title = "Can neovim be used as a service, instead of starting a new instance every time?"
    let label = ChatCardTitle.Label(labelWithString: title)
    label.font = .systemFont(ofSize: 13, weight: .semibold)
    label.frame = NSRect(x: 0, y: 0, width: 180, height: 18)
    label.layout()
    precondition(label.toolTip == title, "Truncated title must expose the complete source string")
    label.frame.size.width = 900
    label.layout()
    precondition(label.toolTip == nil, "A fully visible title should not show redundant help")
    let singleLineHeight = label.intrinsicContentSize.height
    label.stringValue = "A title\nwith a second line"
    label.layout()
    precondition(label.toolTip == "A title\nwith a second line", "Tooltip must preserve hidden lines")
    precondition(label.intrinsicContentSize.height == singleLineHeight,
                 "Paragraph breaks must not increase the compact title height")
}

struct ChatCard: View {
    let chat: PickerChat
    let activity: CardActivity?
    let content: ChatCardContent
    let onFlip: (Bool) -> Void
    @State private var flipped = false
    @State private var hovered = false
    @State private var editedInstruction = ""
    @State private var savedInstruction = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var isQueued: Bool {
        if case .queued = content { return true }
        return false
    }
    private var blocked: Bool {
        if case .queued(let entry, _, _) = content, entry.hasActiveDelivery { return true }
        return activity?.blocksAction == true || content.state == .processing
    }
    private var revealsRemove: Bool { isQueued && hovered && !flipped }
    private var tint: Color {
        switch content.state {
        case .base: return .primary
        case .queued: return .orange
        case .processing: return .blue
        case .failed: return .red
        }
    }
    private var frontHeight: CGFloat {
        if case .queued(let entry, _, _) = content { return entry.cardHeight }
        return 58
    }

    var body: some View {
        ZStack(alignment: .trailing) {
            if case .queued(_, _, let remove) = content {
                Button { Task { _ = await remove() } } label: {
                    Image(systemName: "trash")
                        .font(.system(size: 14, weight: .medium))
                        .frame(width: 36, height: 36)
                        .contentShape(Rectangle())
                }
                .buttonStyle(CardActionStyle(destructive: true))
                .help("Remove from queue").accessibilityLabel("Remove from queue")
                .disabled(blocked)
                .opacity(revealsRemove ? 1 : 0)
                .allowsHitTesting(revealsRemove).accessibilityHidden(!revealsRemove)
            }
            cardSurface.padding(.trailing, revealsRemove ? 44 : 0)
        }
        .contentShape(Rectangle())
        .onHover { hovered = $0 }
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.18), value: hovered)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.28), value: flipped)
        .animation(reduceMotion ? nil : .easeInOut(duration: 0.3), value: content.state)
        .onChange(of: flipped) { value in onFlip(value) }
        .onDisappear { onFlip(false) }
    }

    private var cardSurface: some View {
        ZStack {
            front
                .frame(height: frontHeight)
                .opacity(flipped ? 0 : 1)
                .rotation3DEffect(.degrees(reduceMotion ? 0 : (flipped ? 180 : 0)), axis: (x: 1, y: 0, z: 0))
                .allowsHitTesting(!flipped).accessibilityHidden(flipped)
            back
                .frame(height: 116)
                .opacity(flipped ? 1 : 0)
                .rotation3DEffect(.degrees(reduceMotion ? 0 : (flipped ? 0 : -180)), axis: (x: 1, y: 0, z: 0))
                .allowsHitTesting(flipped).accessibilityHidden(!flipped)
        }
        .frame(height: flipped ? 116 : frontHeight)
        .background(tint.opacity(isQueued ? (hovered ? 0.15 : 0.08) : (hovered ? 0.08 : 0.035)),
                    in: RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13)
            .strokeBorder(tint.opacity(isQueued ? 0.3 : (hovered ? 0.16 : 0.06))))
        .overlay {
            if content.state == .processing {
                TimelineView(.animation(minimumInterval: 1.0 / 30, paused: reduceMotion)) { context in
                    let opacity = reduceMotion ? 0.65 : 0.45 + 0.2 * sin(context.date.timeIntervalSinceReferenceDate * .pi)
                    RoundedRectangle(cornerRadius: 13).strokeBorder(Color.blue.opacity(opacity), lineWidth: 1.5)
                }.allowsHitTesting(false).accessibilityHidden(true)
            }
        }
    }

    @ViewBuilder private var activityIndicator: some View {
        if let activity {
            switch activity {
            case .working:
                ProgressView().controlSize(.mini).help(activity.detail)
            case .uncertain, .failed:
                Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
                    .help(activity.detail).accessibilityLabel(activity.detail)
            case .complete:
                EmptyView()
            }
        }
    }

    @ViewBuilder private var queueStateIndicator: some View {
        if case .queued(let entry, _, _) = content {
            Group {
                switch entry.cardState {
                case .processing:
                    TimelineView(.animation(minimumInterval: 1.0 / 30, paused: reduceMotion)) { context in
                        Circle().trim(from: 0, to: 0.72)
                            .stroke(Color.blue, style: StrokeStyle(lineWidth: 1.5, lineCap: .round))
                            .rotationEffect(.degrees(reduceMotion ? 0 : context.date.timeIntervalSinceReferenceDate * 360))
                            .frame(width: 12, height: 12)
                    }
                case .failed:
                    Image(systemName: "exclamationmark.circle.fill")
                case .queued, .base:
                    Image(systemName: "clock")
                }
            }
            .font(.system(size: 12, weight: .medium)).foregroundStyle(tint)
            .frame(width: 16, height: 16)
            .help(entry.statusDescription).accessibilityLabel(entry.statusDescription)
        }
    }

    private var front: some View {
        Button {
            if case .queued(let entry, _, _) = content {
                editedInstruction = entry.prompt
                savedInstruction = entry.prompt
            }
            flipped = true
        } label: {
            VStack(alignment: .leading, spacing: 0) {
                VStack(alignment: .leading, spacing: 5) {
                    HStack(alignment: .top) {
                        ChatCardTitle(title: chat.displayTitle)
                            .frame(maxWidth: .infinity, alignment: .leading)
                        activityIndicator
                        queueStateIndicator
                    }
                    if case .queued(let entry, _, _) = content {
                        HStack(alignment: .firstTextBaseline, spacing: 5) {
                            Text("“")
                                .font(.system(size: 15, weight: .medium, design: .serif))
                                .foregroundStyle(.tertiary)
                            Text(entry.prompt)
                                .font(.system(size: 11.5)).foregroundStyle(Color.primary.opacity(0.72)).lineLimit(1)
                        }
                        .help(entry.prompt)
                        .accessibilityLabel("Queued instruction: \(entry.prompt)")
                    }
                }
                .padding(.horizontal, 11).padding(.top, 12)

                Spacer(minLength: 6)

                HStack(spacing: 6) {
                    Label(chat.location, systemImage: "folder").lineLimit(1)
                    Spacer(minLength: 2)
                    TimelineView(.periodic(from: .now, by: 60)) { context in
                        Text(chat.activityDate == nil ? "No activity recorded" : "Last active \(chat.activityLabel(at: context.date).lowercased())").lineLimit(1)
                            .help(chat.activityDate.map { "Last activity: \($0.formatted())" } ?? "Activity time unavailable")
                    }.fixedSize()
                }.font(.system(size: 9.5)).foregroundStyle(.secondary)
                    .padding(.horizontal, 11).padding(.bottom, 10)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: frontHeight)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(chat.displayTitle), project \(chat.location), last active \(chat.activityLabel(at: Date())), \(isQueued ? "edit queued instruction" : "edit instruction")")
        .accessibilityValue(statusDescription)
    }

    private var statusDescription: String {
        if case .queued(let entry, _, _) = content { return entry.statusDescription }
        return ""
    }

    private var back: some View {
        ZStack {
            Button { flipped = false } label: {
                Color.clear.contentShape(Rectangle())
            }
            .buttonStyle(.plain).accessibilityLabel("Back to \(chat.displayTitle)")
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    ChatCardTitle(title: chat.displayTitle, size: 11)
                    Spacer()
                    activityIndicator
                    queueStateIndicator
                }
                switch content {
                case .draft(let instruction, let canQueue, let queue):
                    instructionField(instruction, action: "Add to queue", symbol: "arrow.up", canSubmit: canQueue) {
                        Task { if await queue() { flipped = false } }
                    }
                case .queued(let entry, let save, _):
                    instructionField($editedInstruction, action: "Save instruction", symbol: "arrow.up",
                                     canSubmit: !blocked && ["queued", "failed"].contains(entry.state) && editedInstruction != savedInstruction,
                                     editable: ["queued", "failed"].contains(entry.state)) {
                        Task { if await save(editedInstruction, savedInstruction) { flipped = false } }
                    }
                }
            }.padding(11)
        }
    }

    private func instructionField(_ text: Binding<String>, action: String, symbol: String,
                                  canSubmit: Bool, editable: Bool = true,
                                  submit: @escaping () -> Void) -> some View {
        HStack(alignment: .bottom, spacing: 8) {
            ZStack(alignment: .topLeading) {
                if text.wrappedValue.isEmpty {
                    Text(ChatPickerModel.defaultInstruction).font(.system(size: 12))
                        .foregroundStyle(.tertiary).padding(.top, 2).allowsHitTesting(false)
                }
                InstructionEditor(text: text, focused: flipped, enabled: editable && !blocked,
                                  label: "Instruction for \(chat.displayTitle)",
                                  submit: { if canSubmit { submit() } }, cancel: { flipped = false })
            }
            Button(action: submit) {
                Image(systemName: symbol).font(.system(size: 12, weight: .medium))
                    .frame(width: 28, height: 28)
            }
            .buttonStyle(CardActionStyle()).disabled(!canSubmit)
            .help("\(action) · Enter\nShift–Enter for a new line")
            .accessibilityLabel(action)
        }
        .padding(8)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
    }
}

@MainActor
final class ChatPickerPopover: NSObject, NSPopoverDelegate {
    let model: ChatPickerModel
    let popover = NSPopover()
    private let visibilityChanged: () -> Void

    init(baseURL: URL, lifecycle: AppLifecycle, didQueue: @escaping () -> Void, visibilityChanged: @escaping () -> Void) {
        model = ChatPickerModel(baseURL: baseURL, didQueue: didQueue)
        self.visibilityChanged = visibilityChanged
        super.init()
        popover.behavior = .transient
        popover.delegate = self
        popover.animates = !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        // Let NSPopover supply the system material, including Liquid Glass on newer macOS.
        popover.contentViewController = NSHostingController(rootView: ChatPickerView(model: model, lifecycle: lifecycle))
        popover.contentSize = NSSize(width: 440, height: 620)
    }

    func show(relativeTo button: NSStatusBarButton) {
        guard !popover.isShown else { return }
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.title =
            Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Carrie On"
        popover.contentViewController?.view.window?.makeKey()
        Task { await model.load() }
    }

    func popoverShouldDetach(_ popover: NSPopover) -> Bool { false }
    func popoverDidShow(_ notification: Notification) { visibilityChanged() }
    func popoverDidClose(_ notification: Notification) { visibilityChanged() }
}
