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
    case base, queued, failed
}

struct PickerChatPage: Decodable {
    let chats: [PickerChat]
    let next_cursor: String?
}

struct PickerQueue: Decodable {
    struct Entry: Decodable, Identifiable {
        let enrollmentID: String
        let thread_id: String
        let title: String
        let prompt: String
        var state = "waiting"
        var note: String? = nil
        var reactivation_error: String? = nil
        var attempted_at: Double? = nil
        var id: String { thread_id }
        var cardState: ChatCardState {
            reactivation_error != nil || state == "needs_attention" ? .failed : .queued
        }
        var failureTitle: String {
            attempted_at != nil ? "Resume unconfirmed" : "Couldn’t resume"
        }
        var failureDetail: String {
            let reason = reactivation_error ?? note ?? "The chat could not be resumed."
            return reason + (state == "waiting" ? " Will retry automatically." : " Inspect the chat before re-queueing.")
        }
        var cardHeight: CGFloat { cardState == .failed ? 104 : 84 }
        enum CodingKeys: String, CodingKey {
            case enrollmentID = "id"
            case thread_id, title, prompt, state, note, reactivation_error, attempted_at
        }
    }
    let entries: [Entry]
    let paused: Bool
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
    @Published var paused = false
    @Published var ready = false
    @Published var connection: CodexConnection = .checking
    @Published private(set) var openingCodex = false
    @Published private(set) var launchError: String?
    @Published private var checkingConnection = false
    @Published private var loadingUsage = false
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
    private var queueReadRevision = 0
    private let baseURL: URL
    private let session: URLSession
    private let didQueue: () -> Void

    init(baseURL: URL, session: URLSession = .shared, didQueue: @escaping () -> Void) {
        self.baseURL = baseURL
        self.session = session
        self.didQueue = didQueue
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
        activities.values.contains { if case .working = $0 { return true }; return false }
    }
    var refreshingCodex: Bool {
        connection == .connected && (checkingConnection || refreshingSummary || loadingUsage || searching || checkingMetadata)
    }
    func queuedChat(_ entry: PickerQueue.Entry) -> PickerChat {
        knownChats[entry.id] ?? PickerChat(id: entry.id, title: entry.title, cwd: nil)
    }
    func prompt(for id: String) -> String {
        let text = (instructions[id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? Self.defaultInstruction : text
    }
    func canAct(on id: String) -> Bool { activities[id]?.blocksAction != true }
    func canQueue(_ id: String) -> Bool {
        connection == .connected && canAct(on: id) && !queued.contains(id) && (buckets.count <= 1 || !bucket.isEmpty)
    }

    private func request(_ path: String, query: String? = nil, cursor: String? = nil, body: [String: String]? = nil, timeout: Double = 120) async throws -> Data {
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if let query { components.queryItems = [URLQueryItem(name: "search", value: query)] }
        if let cursor { components.queryItems = (components.queryItems ?? []) + [URLQueryItem(name: "cursor", value: cursor)] }
        var request = URLRequest(url: components.url!)
        request.setValue("1", forHTTPHeaderField: "X-Tokenmaxxer")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = timeout
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
        }
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            struct Failure: Decodable { let error: String }
            let reason = (try? JSONDecoder().decode(Failure.self, from: data).error) ?? "Local service unavailable. Try again shortly."
            throw NSError(domain: "Tokenmaxxer", code: 1, userInfo: [NSLocalizedDescriptionKey: reason])
        }
        return data
    }

    func load() async {
        async let queue: () = updateQueue()
        await updateConnection()
        async let catalog: () = searchChats()
        async let usage: () = loadUsage()
        async let metadata: () = loadQueuedMetadata()
        _ = await (catalog, queue, usage, metadata)
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
            await updateConnection()
        } catch {
            launchError = error.localizedDescription
        }
    }

    func updateConnection() async {
        guard !checkingConnection else { return }
        checkingConnection = true
        defer { checkingConnection = false }
        let previous = connection
        let identifiers = ["com.openai.codex", "com.openai.codex.helper"]
        let running = NSWorkspace.shared.runningApplications.contains { identifiers.contains($0.bundleIdentifier ?? "") }
        let installed = running || codexApplicationURL != nil
        if !running { connection = CodexConnection.resolve(installed: installed, running: false, handshake: false) }
        do {
            struct Connection: Decodable { let connected: Bool }
            let data = try await request("api/connection", timeout: 5)
            let handshake = try JSONDecoder().decode(Connection.self, from: data).connected
            connection = CodexConnection.resolve(installed: installed, running: running, handshake: handshake)
        } catch {
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
            let data = try await request("api/queued-chats")
            let results = try JSONDecoder().decode([PickerChat].self, from: data)
            for chat in results {
                if chat.activityDate != nil || knownChats[chat.id] == nil { knownChats[chat.id] = chat }
            }
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
            let data = try await request("api/chat-page", query: query, cursor: cursor)
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
                await updateConnection()
            }
        }
    }

    private func loadUsage() async {
        guard !loadingUsage else { return }
        loadingUsage = true
        defer { loadingUsage = false }
        do {
            let data = try await request("api/menu")
            summary = try MenuSnapshot.decode(data)
        } catch { /* The menu app also refreshes usage independently. */ }
    }

    func reconcile(_ queue: PickerQueue) {
        queuedEntries = queue.entries
        paused = queue.paused
    }

    func updateQueue() async {
        if !ready && !searching { Task { await searchChats() } }
        queueReadRevision += 1
        let readRevision = queueReadRevision
        let mutationRevision = queueRevision
        checkingQueue = true
        defer { if readRevision == queueReadRevision { checkingQueue = false } }
        do {
            let data = try await request("api/status")
            let queue = try JSONDecoder().decode(PickerQueue.self, from: data)
            // A read started before a mutation must not undo the mutation's result.
            guard readRevision == queueReadRevision, mutationRevision == queueRevision, !working else { return }
            reconcile(queue)
            Task { await loadQueuedMetadata() }
            for (id, activity) in activities {
                if case .uncertain(let reason) = activity {
                    activities[id] = .failed(reason + " Queue status has been refreshed.")
                }
            }
        } catch { /* Existing cards stay usable; unknown writes remain blocked on their own card. */ }
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
        guard prompt.count <= 32000 else {
            activities[chat.id] = .failed("Keep the instruction under 32,000 characters.")
            return false
        }
        var body = ["chat": chat.id, "prompt": prompt]
        if !bucket.isEmpty { body["limit_id"] = bucket }
        begin(chat.id, detail: "Queueing…")
        do {
            struct Enrollment: Decodable { let entry: PickerQueue.Entry }
            let data = try await request("api/queue", body: body)
            let entry = try JSONDecoder().decode(Enrollment.self, from: data).entry
            queuedEntries.removeAll { $0.enrollmentID == entry.enrollmentID }
            queuedEntries.append(entry)
            finish(chat.id, result: .complete("Queued"))
            return true
        } catch {
            finish(chat.id, result: .uncertain(error.localizedDescription))
            return false
        }
    }

    func changeQueued(_ entry: PickerQueue.Entry, change: QueuedChange) async -> Bool {
        guard canAct(on: entry.id) else { return false }
        begin(entry.id, detail: "Updating…")
        do {
            switch change {
            case .instruction(let text, let expected):
                let prompt = text.trimmingCharacters(in: .whitespacesAndNewlines)
                let data = try await request("api/instruction", body: [
                    "chat": entry.enrollmentID,
                    "prompt": prompt.isEmpty ? Self.defaultInstruction : prompt,
                    "expected_prompt": expected
                ])
                struct Enrollment: Decodable { let entry: PickerQueue.Entry }
                let saved = try JSONDecoder().decode(Enrollment.self, from: data).entry
                if let index = queuedEntries.firstIndex(where: { $0.enrollmentID == entry.enrollmentID }) {
                    queuedEntries[index] = saved
                }
            case .remove:
                _ = try await request("api/remove", body: ["chat": entry.enrollmentID])
                instructions[entry.id] = entry.prompt
                queuedEntries.removeAll { $0.enrollmentID == entry.enrollmentID }
            }
            finish(entry.id, result: .complete("Updated"))
            return true
        } catch {
            finish(entry.id, result: .uncertain(error.localizedDescription))
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
                    Text("Carrie On").font(.system(size: 18, weight: .semibold))
                    Spacer()
                    Menu {
                        Toggle("Launch at Login", isOn: Binding(
                            get: { lifecycle.launchAtLogin },
                            set: { lifecycle.setLaunchAtLogin($0) }))
                            .disabled(lifecycle.busy || !lifecycle.ready)
                        Divider()
                        Button("Quit Carrie On") { lifecycle.quit() }
                            .disabled(lifecycle.busy)
                    } label: {
                        Image(systemName: "gearshape")
                            .font(.system(size: 15, weight: .medium))
                            .frame(width: 24, height: 24)
                    }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
                    .accessibilityLabel("Settings")
                    .help("Settings")
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
                    ScrollView {
                        LazyVStack(spacing: 6) {
                            ForEach(model.matchingQueuedEntries) { entry in
                                ChatCard(chat: model.queuedChat(entry), activity: model.activities[entry.id],
                                    content: .queued(entry,
                                        save: { text, expected in
                                            await model.changeQueued(entry, change: .instruction(text, expected: expected))
                                        }, remove: { await model.changeQueued(entry, change: .remove) }),
                                    onFlip: { flipped in
                                        if flipped { expandedQueued.insert(entry.enrollmentID) }
                                        else { expandedQueued.remove(entry.enrollmentID) }
                                    })
                                    .id("queued-" + entry.enrollmentID)
                                    .transition(cardTransition)
                            }
                        }.padding(.bottom, 6)
                    }
                    .frame(height: queuedHeight)
                    .padding(.top, 6)
                    .accessibilityLabel("Queued chats")
                }
            } label: {
                Label("Queued · \(model.matchingQueuedEntries.count)", systemImage: "hourglass")
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            }.padding(.horizontal, 20).padding(.bottom, 12)

            DisclosureGroup(isExpanded: $showChats) {
                VStack(spacing: 8) {
                    if model.buckets.count > 1 {
                        Picker("Usage bucket", selection: $model.bucket) {
                            Text("Select a bucket").tag("")
                            ForEach(model.buckets, id: \.self) { Text($0).tag($0) }
                        }
                    }

                    ScrollView {
                        LazyVStack(spacing: 6) {
                            ForEach(model.availableChats) { chat in
                                ChatCard(chat: chat, activity: model.activities[chat.id], content: .draft(
                                    instruction: Binding(
                                        get: { model.instructions[chat.id] ?? "" },
                                        set: { model.instructions[chat.id] = $0 }),
                                    canQueue: model.canQueue(chat.id),
                                    queue: { await model.queueChat(chat) }), onFlip: { _ in })
                                    .id("draft-" + chat.id)
                                    .transition(cardTransition)
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
                        }.padding(.bottom, 12)
                    }.accessibilityLabel("Available chats")
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
            .help(connectionError ?? (model.refreshingCodex ? "Updating usage and chats in the background." : model.connection.detail))
            Spacer(minLength: 0)
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
            if model.paused {
                Label("Queue paused", systemImage: "pause.circle").font(.caption).foregroundStyle(.orange)
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
    private var blocked: Bool { activity?.blocksAction == true }
    private var revealsRemove: Bool { isQueued && hovered && !flipped }
    private var tint: Color {
        switch content.state {
        case .base: return .primary
        case .queued: return .orange
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
                .opacity(flipped ? 0 : 1)
                .rotation3DEffect(.degrees(reduceMotion ? 0 : (flipped ? 180 : 0)), axis: (x: 1, y: 0, z: 0))
                .allowsHitTesting(!flipped).accessibilityHidden(flipped)
            back
                .opacity(flipped ? 1 : 0)
                .rotation3DEffect(.degrees(reduceMotion ? 0 : (flipped ? 0 : -180)), axis: (x: 1, y: 0, z: 0))
                .allowsHitTesting(flipped).accessibilityHidden(!flipped)
        }
        .frame(height: flipped ? 116 : frontHeight)
        .background(tint.opacity(isQueued ? (hovered ? 0.15 : 0.08) : (hovered ? 0.08 : 0.035)),
                    in: RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13)
            .strokeBorder(tint.opacity(isQueued ? 0.3 : (hovered ? 0.16 : 0.06))))
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

    private var front: some View {
        Button {
            if case .queued(let entry, _, _) = content {
                editedInstruction = entry.prompt
                savedInstruction = entry.prompt
            }
            flipped = true
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .top) {
                    Text(chat.displayTitle).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    activityIndicator
                }
                HStack(spacing: 6) {
                    Label(chat.location, systemImage: "folder").lineLimit(1)
                    Spacer(minLength: 2)
                    TimelineView(.periodic(from: .now, by: 60)) { context in
                        Text(chat.activityLabel(at: context.date)).lineLimit(1)
                            .help(chat.activityDate.map { "Last activity: \($0.formatted())" } ?? "Activity time unavailable")
                    }.fixedSize()
                }.font(.caption2).foregroundStyle(.secondary)
                if case .queued(let entry, _, _) = content {
                    Label(entry.prompt, systemImage: "hourglass")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    if entry.cardState == .failed {
                        Label(entry.failureTitle, systemImage: "exclamationmark.circle.fill")
                            .font(.caption2.weight(.medium)).foregroundStyle(.red)
                        Text(entry.reactivation_error ?? entry.note ?? "The chat could not be resumed.")
                            .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                            .help(entry.failureDetail)
                    } else if entry.state != "waiting" {
                        Text(entry.note ?? entry.state).font(.caption2).foregroundStyle(.orange).lineLimit(1)
                    }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .padding(9).contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(chat.displayTitle), \(chat.location), \(chat.activityLabel(at: Date())), \(isQueued ? "edit queued instruction" : "edit instruction")")
        .accessibilityValue(failureDescription)
        .help(failureDescription)
    }

    private var failureDescription: String {
        if case .queued(let entry, _, _) = content, entry.cardState == .failed {
            return "\(entry.failureTitle). \(entry.failureDetail)"
        }
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
                    Text(chat.displayTitle).font(.caption.weight(.semibold)).lineLimit(1)
                        .allowsHitTesting(false)
                    Spacer()
                    activityIndicator
                }
                switch content {
                case .draft(let instruction, let canQueue, let queue):
                    instructionField(instruction, action: "Add to queue", symbol: "arrow.up", canSubmit: canQueue) {
                        Task { if await queue() { flipped = false } }
                    }
                case .queued(let entry, let save, _):
                    instructionField($editedInstruction, action: "Save instruction", symbol: "arrow.up",
                                     canSubmit: !blocked && entry.state == "waiting" && editedInstruction != savedInstruction,
                                     editable: entry.state == "waiting") {
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
        popover.contentViewController?.view.window?.title = "Carrie On"
        popover.contentViewController?.view.window?.makeKey()
        Task { await model.load() }
    }

    func popoverShouldDetach(_ popover: NSPopover) -> Bool { false }
    func popoverDidShow(_ notification: Notification) { visibilityChanged() }
    func popoverDidClose(_ notification: Notification) { visibilityChanged() }
}
