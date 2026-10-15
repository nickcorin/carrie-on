import Foundation

final class ChatPageProtocol: URLProtocol {
    @MainActor static var pending: [ChatPageProtocol] = []
    @MainActor static var requests = 0
    @MainActor static var connected = true
    @MainActor static var menuRequests = 0
    @MainActor static var usageAvailable = true
    @MainActor static var menuStatus = 200
    @MainActor static var queueJSON = "[]"
    @MainActor static var queuePages: [String: String] = [:]

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Task { @MainActor in
            if request.url!.path == "/api/v1/status" {
                Self.menuRequests += 1
                let usage = Self.usageAvailable ? #"{"windows":[{"bucket":"codex","name":"primary","used_percent":25,"resets_at":2000000000}]}"# : "null"
                reply("{\"connected\":\(Self.connected),\"usage\":\(usage),\"queue\":{\"count\":0,\"needs_attention\":0}}", status: Self.menuStatus)
            } else if request.url!.path == "/api/v1/connection" {
                fatalError("Refresh must not make a separate connection request")
            } else if request.url!.path == "/api/v1/queue", request.httpMethod == "GET" {
                let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
                    .queryItems?.first(where: { $0.name == "cursor" })?.value ?? ""
                reply(Self.queuePages[cursor] ?? "{\"items\":\(Self.queueJSON),\"next_cursor\":null}")
            } else if request.url!.path == "/api/v1/queue/metadata" {
                reply(#"{"items":[],"next_cursor":null}"#)
            } else if request.url!.host == "queue.test", request.url!.path == "/api/v1/chats" {
                reply(#"{"items":[],"next_cursor":null}"#)
            } else {
                Self.requests += 1
                Self.pending.append(self)
            }
        }
    }
    override func stopLoading() {}

    func reply(_ json: String, status: Int = 200) {
        let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                       httpVersion: "HTTP/1.1", headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(json.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    @MainActor static func next() async throws -> ChatPageProtocol {
        for _ in 0..<200 {
            if !pending.isEmpty { return pending.removeFirst() }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        fatalError("Expected chat page request did not arrive")
    }
}

func requestBody(_ request: URLRequest) throws -> [String: String] {
    var data = request.httpBody ?? Data()
    if let stream = request.httpBodyStream {
        stream.open()
        defer { stream.close() }
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(contentsOf: buffer.prefix(count))
        }
    }
    guard !data.isEmpty else { fatalError("Missing body for \(request.httpMethod ?? "GET") \(request.url!)") }
    return try JSONDecoder().decode([String: String].self, from: data)
}

@MainActor
func waitForQueueRead(_ model: ChatPickerModel) async throws {
    for _ in 0..<200 {
        if !model.checkingQueue && !model.working { return }
        try await Task.sleep(nanoseconds: 5_000_000)
    }
    fatalError("Queue reconciliation did not finish")
}

@MainActor
func testQueueActions() async throws {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [ChatPageProtocol.self]
    let session = URLSession(configuration: configuration)
    defer { session.invalidateAndCancel() }
    let model = ChatPickerModel(baseURL: URL(string: "http://queue.test")!, session: session, recoveryDefaults: nil, didQueue: {})
    model.connection = .connected
    let chat = PickerChat(id: UUID().uuidString.lowercased(), title: "Queue HTTP checks", cwd: nil)

    func item(_ id: String, instruction: String, revision: Int, state: String = "queued") throws -> String {
        let fields: [String: Any] = ["id": id, "chat_id": chat.id, "title": chat.title!,
                                    "instruction": instruction, "revision": revision, "state": state]
        return String(decoding: try JSONSerialization.data(withJSONObject: fields), as: UTF8.self)
    }
    func settle() async throws {
        for _ in 0..<200 {
            if !model.working && model.canAct(on: chat.id) { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        fatalError("Queue action did not reconcile")
    }

    let add = Task { await model.queueChat(chat) }
    let creation = try await ChatPageProtocol.next()
    let payload = try requestBody(creation.request)
    guard creation.request.url!.path == "/api/v1/queue", let id = payload["id"], UUID(uuidString: id) != nil,
          payload["chat_id"] == chat.id, payload["instruction"] == ChatPickerModel.defaultInstruction else {
        fatalError("Native creation did not use the queue contract")
    }
    ChatPageProtocol.queueJSON = "[\(try item(id, instruction: ChatPickerModel.defaultInstruction, revision: 1))]"
    creation.reply(#"{"detail":"Lost reply"}"#, status: 503)
    guard await add.value == false else { fatalError("Failed reply was reported as confirmed") }
    try await settle()
    guard let entry = model.queuedEntries.first, entry.enrollmentID == id else {
        fatalError("Uncertain creation was not reconciled")
    }

    let edit = Task { await model.changeQueued(entry, change: .instruction("Revised", expected: entry.prompt)) }
    let editing = try await ChatPageProtocol.next()
    guard editing.request.httpMethod == "PATCH", editing.request.value(forHTTPHeaderField: "If-Match") == "\"revision-1\"",
          editing.request.value(forHTTPHeaderField: "Content-Type") == "application/merge-patch+json" else {
        fatalError("Native edits lost their revision or content type: \(editing.request.httpMethod ?? "nil") \(editing.request.url!) \(editing.request.allHTTPHeaderFields ?? [:])")
    }
    let saved = try item(id, instruction: "Revised", revision: 2)
    ChatPageProtocol.queueJSON = "[\(saved)]"
    editing.reply(saved)
    guard await edit.value else { fatalError("Instruction edit failed") }
    try await settle()
    let remove = Task { await model.changeQueued(model.queuedEntries[0], change: .remove) }
    let removing = try await ChatPageProtocol.next()
    guard removing.request.url!.path == "/api/v1/queue/\(id)/cancel" else { fatalError("Wrong cancellation resource") }
    ChatPageProtocol.queueJSON = "[]"
    removing.reply(try item(id, instruction: "Revised", revision: 3, state: "cancelled"))
    guard await remove.value else { fatalError("Cancellation failed") }
    try await settle()
    let requeue = Task { await model.queueChat(chat) }
    let replacement = try await ChatPageProtocol.next()
    let replacementID = try requestBody(replacement.request)["id"]!
    guard replacementID != id else { fatalError("Resolved creation reused a cancelled item's UUID") }
    let replacementItem = try item(replacementID, instruction: "Revised", revision: 1)
    ChatPageProtocol.queueJSON = "[\(replacementItem)]"
    replacement.reply(replacementItem, status: 201)
    guard await requeue.value else { fatalError("Re-enrollment failed") }
    try await settle()
}

@MainActor
func testUncertainCreationRecovery() async throws {
    ChatPageProtocol.queueJSON = "[]"
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [ChatPageProtocol.self]
    let session = URLSession(configuration: configuration)
    let suite = "carrie-on-native-tests-\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer {
        session.invalidateAndCancel()
        defaults.removePersistentDomain(forName: suite)
    }
    let baseURL = URL(string: "http://queue.test")!
    let model = ChatPickerModel(baseURL: baseURL, session: session, recoveryDefaults: defaults, didQueue: {})
    model.connection = .connected
    model.ready = true
    let chat = PickerChat(id: UUID().uuidString.lowercased(), title: "Recover original request", cwd: nil)
    model.instructions[chat.id] = "Original instruction"
    model.bucket = "original-bucket"

    let create = Task { await model.queueChat(chat) }
    let posting = try await ChatPageProtocol.next()
    guard posting.request.httpMethod == "POST" else { fatalError("Initial creation received \(posting.request)") }
    let original = try requestBody(posting.request)
    guard original["instruction"] == "Original instruction", original["limit_id"] == "original-bucket",
          let itemID = original["id"] else { fatalError("Original request input was lost") }
    posting.reply(#"{"code":"unavailable","detail":"Lost response"}"#, status: 503)
    guard await create.value == false else { fatalError("An unknown create was reported as confirmed") }
    let unavailable = try await ChatPageProtocol.next()
    guard unavailable.request.url!.path == "/api/v1/queue/\(itemID)" else { fatalError("Recovery did not read the original UUID") }
    model.instructions[chat.id] = "Changed draft"
    model.bucket = "changed-bucket"
    unavailable.reply(#"{"code":"unavailable","detail":"Read failed"}"#, status: 503)
    try await waitForQueueRead(model)
    guard !model.canQueue(chat.id), model.canQueue(UUID().uuidString),
          model.activity(for: chat.id)?.detail.contains("previous request") == true else {
        fatalError("Failed item lookup enabled a second send or blocked unrelated chats")
    }

    let refresh = Task { await model.updateQueue(refreshMetadata: false) }
    let missing = try await ChatPageProtocol.next()
    missing.reply(#"{"code":"not_found","detail":"Queue item not found"}"#, status: 404)
    await refresh.value
    guard model.canQueue(chat.id) else { fatalError("Confirmed absence did not permit an idempotent retry") }
    let retry = Task { await model.queueChat(chat) }
    let retrying = try await ChatPageProtocol.next()
    guard retrying.request.httpMethod == "POST" else { fatalError("Original retry received \(retrying.request)") }
    guard try requestBody(retrying.request) == original else { fatalError("Retry used changed instruction, bucket, or identity") }
    retrying.reply(#"{"code":"id_reused","detail":"Existing creation has a different payload"}"#, status: 409)
    guard await retry.value == false else { fatalError("Conflicting identity was reported as confirmed") }
    let stillUnknown = try await ChatPageProtocol.next()
    guard stillUnknown.request.url!.path == "/api/v1/queue/\(itemID)" else { fatalError("id_reused discarded recovery identity") }
    stillUnknown.reply(#"{"code":"unavailable","detail":"Read failed again"}"#, status: 503)
    try await waitForQueueRead(model)
    guard !model.canQueue(chat.id) else { fatalError("id_reused enabled creation with a fresh UUID") }

    let restarted = ChatPickerModel(baseURL: baseURL, session: session, recoveryDefaults: defaults, didQueue: {})
    restarted.connection = .connected
    restarted.ready = true
    restarted.instructions[chat.id] = "Draft after restart"
    restarted.bucket = "bucket-after-restart"
    guard !restarted.canQueue(chat.id) else { fatalError("Restart lost unknown creation state") }
    let recover = Task { await restarted.updateQueue(refreshMetadata: false) }
    let missingAfterRestart = try await ChatPageProtocol.next()
    guard missingAfterRestart.request.url!.path == "/api/v1/queue/\(itemID)" else { fatalError("Restart lost original identity") }
    missingAfterRestart.reply(#"{"code":"not_found","detail":"Queue item not found"}"#, status: 404)
    await recover.value
    let retryAfterRestart = Task { await restarted.queueChat(chat) }
    let restored = try await ChatPageProtocol.next()
    guard restored.request.httpMethod == "POST" else { fatalError("Restart retry received \(restored.request)") }
    guard try requestBody(restored.request) == original else { fatalError("Restart changed the original creation payload") }
    restored.reply("""
        {"id":"\(itemID)","chat_id":"\(chat.id)","title":"Recovered chat", "instruction":"Later edited instruction","revision":3,"state":"sent"}
        """)
    guard await retryAfterRestart.value else { fatalError("Completed creation replay was not recovered") }
    try await Task.sleep(nanoseconds: 20_000_000)
    try await waitForQueueRead(restarted)
    guard restarted.queuedEntries.isEmpty, restarted.activity(for: chat.id) == .complete("Previous request: sent.") else {
        fatalError("A completed original request was shown as newly queued")
    }
    let cleanRestart = ChatPickerModel(baseURL: baseURL, session: session, recoveryDefaults: defaults, didQueue: {})
    cleanRestart.connection = .connected
    guard cleanRestart.canQueue(chat.id) else { fatalError("Confirmed creation remained unresolved after restart") }
    print("Immutable creation and restart recovery checks passed")
}

@MainActor
func testIndependentCreationRecovery() async throws {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [ChatPageProtocol.self]
    let session = URLSession(configuration: configuration)
    let suite = "carrie-on-native-tests-\(UUID().uuidString)"
    let defaults = UserDefaults(suiteName: suite)!
    defer {
        session.invalidateAndCancel()
        defaults.removePersistentDomain(forName: suite)
        ChatPageProtocol.queueJSON = "[]"
    }
    let confirmedID = UUID().uuidString.lowercased()
    let unknownID = UUID().uuidString.lowercased()
    let confirmedChat = UUID().uuidString.lowercased()
    let unknownChat = UUID().uuidString.lowercased()
    defaults.set(Data("""
        {"version":1,"requests":[
          {"id":"\(confirmedID)","chatID":"\(confirmedChat)","instruction":"Confirmed instruction"},
          {"id":"\(unknownID)","chatID":"\(unknownChat)","instruction":"Unknown instruction"}
        ]}
        """.utf8), forKey: "pendingQueueCreations.v1")
    ChatPageProtocol.queueJSON = """
        [{"id":"\(confirmedID)","chat_id":"\(confirmedChat)","title":"Confirmed chat", "instruction":"Confirmed instruction","revision":1,"state":"queued"}]
        """
    let model = ChatPickerModel(baseURL: URL(string: "http://queue.test")!, session: session,
                                recoveryDefaults: defaults, didQueue: {})
    model.ready = true
    model.connection = .connected
    let refresh = Task { await model.updateQueue(refreshMetadata: false) }
    let unresolved = try await ChatPageProtocol.next()
    guard unresolved.request.url!.path == "/api/v1/queue/\(unknownID)" else { fatalError("Confirmed item was needlessly looked up again") }
    unresolved.reply(#"{"code":"unavailable","detail":"Read failed"}"#, status: 503)
    await refresh.value
    guard model.activity(for: confirmedChat) == .complete("Queued"), model.canAct(on: confirmedChat),
          !model.canAct(on: unknownChat), model.queuedEntries.map(\.id) == [confirmedID] else {
        fatalError("A partial reconciliation resolved or blocked the wrong card")
    }
    let isolated = ChatPickerModel(baseURL: URL(string: "http://queue.test")!, session: session,
                                   recoveryDefaults: nil, didQueue: {})
    isolated.connection = .connected
    guard isolated.canQueue(unknownChat) else { fatalError("Creation recovery crossed preference stores") }
    defaults.set(Data(#"{"version":2,"requests":[]}"#.utf8), forKey: "pendingQueueCreations.v1")
    let incompatible = ChatPickerModel(baseURL: URL(string: "http://queue.test")!, session: session,
                                      recoveryDefaults: defaults, didQueue: {})
    incompatible.connection = .connected
    guard incompatible.queueRecoveryError != nil, !incompatible.canQueue(unknownChat),
          incompatible.canAct(on: confirmedChat) else {
        fatalError("An unreadable creation journal silently discarded identity or blocked saved queue actions")
    }
    print("Independent creation recovery checks passed")
}

@MainActor
func testQueueHTTPContract() async throws {
    let fixtureURL = URL(fileURLWithPath: CommandLine.arguments[1])
    let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: fixtureURL)) as! [String: Any]
    func json(_ value: Any) throws -> String {
        String(decoding: try JSONSerialization.data(withJSONObject: value), as: UTF8.self)
    }
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [ChatPageProtocol.self]
    let session = URLSession(configuration: configuration)
    defer { session.invalidateAndCancel(); ChatPageProtocol.queuePages = [:] }
    let model = ChatPickerModel(baseURL: URL(string: "http://queue.test")!, session: session,
                                recoveryDefaults: nil, didQueue: {})
    model.ready = true
    ChatPageProtocol.queuePages = ["": try json(fixture["delivery_states"]!)]
    await model.updateQueue(refreshMetadata: false)
    let entries = Dictionary(uniqueKeysWithValues: model.queuedEntries.map { ($0.title, $0) })
    guard let checking = entries["Checking"], let sending = entries["Sending"],
          let expired = entries["Expired"], let waiting = entries["Waiting"],
          checking.processing_until == 4_000_000_120, checking.attempted_at == nil,
          checking.hasActiveDelivery, checking.cardState == .queued,
          sending.attempted_at == 4_000_000_000.25, sending.processing_until == 4_000_000_120,
          sending.hasActiveDelivery, sending.cardState == .processing,
          expired.attempted_at == 100.25, expired.processing_until == 220,
          !expired.hasActiveDelivery, expired.cardState == .queued,
          waiting.attempted_at == nil, waiting.processing_until == nil,
          !waiting.hasActiveDelivery, waiting.cardState == .queued else {
        fatalError("Actual HTTP delivery timestamps did not produce the expected native states")
    }
    guard await model.changeQueued(checking, change: .remove) == false,
          await model.changeQueued(sending, change: .instruction("Blocked edit", expected: sending.prompt)) == false else {
        fatalError("An active delivery allowed editing or cancellation")
    }
    let edit = Task { await model.changeQueued(expired, change: .instruction("Recovered edit", expected: expired.prompt)) }
    let editing = try await ChatPageProtocol.next()
    guard editing.request.httpMethod == "PATCH",
          editing.request.url!.path == "/api/v1/queue/\(expired.enrollmentID)",
          try requestBody(editing.request)["instruction"] == "Recovered edit" else {
        fatalError("An expired delivery remained locked or edited the wrong queue item")
    }
    var response = (fixture["delivery_states"] as! [String: Any])["items"] as! [[String: Any]]
    let changedIndex = response.firstIndex(where: { $0["id"] as? String == expired.enrollmentID })!
    response[changedIndex]["instruction"] = "Recovered edit"
    response[changedIndex]["revision"] = expired.revision + 1
    ChatPageProtocol.queuePages = ["": try json(["items": response, "next_cursor": NSNull()])]
    editing.reply(try json(response[changedIndex]))
    guard await edit.value else { fatalError("Expired delivery edit failed") }
    try await Task.sleep(nanoseconds: 20_000_000)
    try await waitForQueueRead(model)
    guard model.queuedEntries.first(where: { $0.enrollmentID == expired.enrollmentID })?.prompt == "Recovered edit" else {
        fatalError("Expired delivery edit was not displayed")
    }

    let pages = fixture["queue_pages"] as! [[String: Any]]
    let cursor = pages[0]["next_cursor"] as! String
    ChatPageProtocol.queuePages = ["": try json(pages[0]), cursor: try json(pages[1])]
    await model.updateQueue(refreshMetadata: false)
    guard model.queuedEntries.count == 52, Set(model.queuedEntries.map(\.id)).count == 52,
          Set(model.queuedEntries.map(\.thread_id)).count == 51,
          model.queuedEntries.contains(where: { $0.prompt == "Replacement instruction" }) else {
        fatalError("Cancellation and re-enrollment between HTTP pages duplicated a SwiftUI identity")
    }
    print("Actual HTTP delivery states and queue identity checks passed")
}

@MainActor
func testChatPagination() async throws {
    let now = Date()
    guard RefreshTimestamp.label(nil, at: now) == "Last updated: Never",
          RefreshTimestamp.label(now, at: now) == "Last updated: Just now",
          RefreshTimestamp.label(now.addingTimeInterval(-120), at: now) == "Last updated: 2m ago",
          RefreshTimestamp.label(now.addingTimeInterval(-7200), at: now).hasSuffix(
            now.addingTimeInterval(-7200).formatted(date: .omitted, time: .shortened)) else {
        fatalError("Refresh labels did not use relative or minute-precision times")
    }
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [ChatPageProtocol.self]
    let session = URLSession(configuration: configuration)
    defer { session.invalidateAndCancel() }
    let model = ChatPickerModel(baseURL: URL(string: "http://pagination.test")!, session: session, recoveryDefaults: nil, didQueue: {})
    model.connection = .connected
    guard model.lastUpdatedAt == nil else {
        fatalError("Refresh timestamps must start unknown")
    }
    async let initialSnapshot: () = model.refreshSnapshot()
    async let concurrentRefresh: () = model.refreshSnapshot()
    _ = await (initialSnapshot, concurrentRefresh)
    guard model.connection == .connected, model.lastUpdatedAt != nil, ChatPageProtocol.menuRequests == 1 else {
        fatalError("Concurrent refreshes must share a single snapshot request")
    }
    let updatedAt = model.lastUpdatedAt
    model.summary = nil
    guard model.lastUpdatedAt == updatedAt else {
        fatalError("Rendering changed the successful snapshot timestamp")
    }
    model.instructions["a"] = "Keep this draft."
    model.queuedEntries = [.init(enrollmentID: "queued-a", thread_id: "a", title: "Alpha", prompt: "Continue")]

    let first = Task { await model.searchChats() }
    let firstRequest = try await ChatPageProtocol.next()
    guard firstRequest.request.url!.path == "/api/v1/chats", model.searching else {
        fatalError("First page did not enter loading state")
    }
    firstRequest.reply(#"{"items":[{"id":"a","title":"Alpha","last_activity":300},{"id":"b","title":"Beta","last_activity":200}],"next_cursor":"older+/=&"}"#)
    await first.value
    guard model.availableChats.map(\.id) == ["b"], model.nextCursor == "older+/=&", !model.searching,
          ChatPageProtocol.requests == 1 else { fatalError("First page fetched ahead or changed queue filtering") }

    let more = Task { await model.loadMoreChats() }
    let moreRequest = try await ChatPageProtocol.next()
    let parameters = URLComponents(url: moreRequest.request.url!, resolvingAgainstBaseURL: false)!.queryItems!
    guard parameters.first(where: { $0.name == "cursor" })?.value == "older+/=&",
          model.searching else {
        fatalError("Opaque cursor was not preserved")
    }
    await model.loadMoreChats()
    guard ChatPageProtocol.requests == 2 else { fatalError("Concurrent load requested the same page twice") }
    moreRequest.reply(#"{"detail":"Try again"}"#, status: 503)
    await more.value
    guard model.availableChats.map(\.id) == ["b"], model.nextCursor == "older+/=&",
          model.loadError != nil, !model.searching else {
        fatalError("Page failure discarded results, retry cursor, or failure feedback")
    }

    let retry = Task { await model.loadMoreChats() }
    let retryRequest = try await ChatPageProtocol.next()
    retryRequest.reply(#"{"items":[{"id":"b","title":"Beta","last_activity":200},{"id":"c","title":"Gamma","last_activity":100}],"next_cursor":null}"#)
    await retry.value
    await model.loadMoreChats()
    guard model.availableChats.map(\.id) == ["b", "c"], model.nextCursor == nil,
          model.loadError == nil, ChatPageProtocol.requests == 3,
          model.instructions["a"] == "Keep this draft.", model.queued == ["a"] else {
        fatalError("Retry, deduplication, exhaustion, or draft retention failed")
    }

    let refresh = Task { await model.searchChats() }
    let refreshRequest = try await ChatPageProtocol.next()
    refreshRequest.reply(#"{"items":[{"id":"b","title":"Beta","last_activity":200}],"next_cursor":"stale-page"}"#)
    await refresh.value
    let stalePage = Task { await model.loadMoreChats() }
    let staleRequest = try await ChatPageProtocol.next()
    model.search = "Gamma"
    // A response arriving during the debounce must already be stale.
    staleRequest.reply(#"{"items":[{"id":"wrong","title":"Gamma obsolete"}],"next_cursor":"wrong"}"#)
    await stalePage.value
    guard model.nextCursor == nil, !model.searching,
          !model.chats.contains(where: { $0.id == "wrong" }) else {
        fatalError("Changing search accepted an old page during debounce")
    }

    let gamma = Task { await model.searchChats() }
    let gammaRequest = try await ChatPageProtocol.next()
    model.search = "Beta"
    let beta = Task { await model.searchChats() }
    let betaRequest = try await ChatPageProtocol.next()
    betaRequest.reply(#"{"items":[{"id":"b","title":"Beta","last_activity":200}],"next_cursor":"beta-page"}"#)
    await beta.value
    gammaRequest.reply(#"{"items":[{"id":"c","title":"Gamma"}],"next_cursor":"gamma-page"}"#)
    await gamma.value
    guard model.availableChats.map(\.id) == ["b"], model.nextCursor == "beta-page", !model.searching else {
        fatalError("Older search overwrote the current search")
    }
    let betaMore = Task { await model.loadMoreChats() }
    let betaMoreRequest = try await ChatPageProtocol.next()
    let betaParameters = URLComponents(url: betaMoreRequest.request.url!, resolvingAgainstBaseURL: false)!.queryItems!
    guard betaParameters.first(where: { $0.name == "search" })?.value == "Beta",
          betaParameters.first(where: { $0.name == "cursor" })?.value == "beta-page" else {
        fatalError("Next page lost its search scope")
    }
    betaMoreRequest.reply(#"{"items":[],"next_cursor":"beta-last"}"#)
    await betaMore.value
    guard model.nextCursor == "beta-last", model.availableChats.map(\.id) == ["b"],
          !model.searching else {
        fatalError("Empty page was incorrectly treated as exhaustion")
    }

    model.search = ""
    let cleared = Task { await model.searchChats() }
    let clearedRequest = try await ChatPageProtocol.next()
    clearedRequest.reply(#"{"items":[{"id":"a","title":"Alpha"},{"id":"c","title":"Gamma"}],"next_cursor":null}"#)
    await cleared.value
    guard model.availableChats.map(\.id) == ["c"], model.queued == ["a"],
          model.instructions["a"] == "Keep this draft." else { fatalError("Clearing search failed to restart pagination") }
    let previousUpdate = model.lastUpdatedAt!
    ChatPageProtocol.usageAvailable = false
    await model.refreshSnapshot()
    guard model.connection == .connected, model.summary?.usage == nil, model.lastUpdatedAt == previousUpdate else {
        fatalError("A partial refresh changed the last complete update time")
    }
    ChatPageProtocol.connected = false
    await model.refreshSnapshot()
    guard model.connection != .connected, model.lastUpdatedAt == previousUpdate else {
        fatalError("A failed handshake retained connected status or refreshed the timestamp")
    }
    ChatPageProtocol.menuStatus = 503
    await model.refreshSnapshot()
    guard model.connection != .connected, model.summary == nil, model.lastUpdatedAt == previousUpdate else {
        fatalError("A failed poll made the last update look fresh")
    }
    print("Chat pagination checks passed")
}

Task { @MainActor in
    do {
        try selfTest()
        try await testChatPagination()
        try await testQueueActions()
        try await testUncertainCreationRecovery()
        try await testIndependentCreationRecovery()
        if CommandLine.arguments.count > 1 { try await testQueueHTTPContract() }
        exit(0)
    } catch {
        fputs("\(error)\n", stderr)
        exit(1)
    }
}
RunLoop.main.run()
