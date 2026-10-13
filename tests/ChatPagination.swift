import Foundation

final class ChatPageProtocol: URLProtocol {
    @MainActor static var pending: [ChatPageProtocol] = []
    @MainActor static var requests = 0

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Task { @MainActor in
            if request.url!.path == "/api/connection" {
                reply(#"{"connected":true}"#)
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

@MainActor
func testChatPagination() async throws {
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [ChatPageProtocol.self]
    let session = URLSession(configuration: configuration)
    defer { session.invalidateAndCancel() }
    let model = ChatPickerModel(baseURL: URL(string: "http://pagination.test")!, session: session, didQueue: {})
    model.connection = .connected
    model.instructions["a"] = "Keep this draft."
    model.reconcile(PickerQueue(entries: [.init(enrollmentID: "queued-a", thread_id: "a",
                                               title: "Alpha", prompt: "Continue")], paused: false))

    let first = Task { await model.searchChats() }
    let firstRequest = try await ChatPageProtocol.next()
    guard firstRequest.request.url!.path == "/api/chat-page", model.searching else {
        fatalError("First page did not enter loading state")
    }
    firstRequest.reply(#"{"chats":[{"id":"a","title":"Alpha","last_activity":300},{"id":"b","title":"Beta","last_activity":200}],"next_cursor":"older+/=&"}"#)
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
    moreRequest.reply(#"{"error":"Try again"}"#, status: 503)
    await more.value
    guard model.availableChats.map(\.id) == ["b"], model.nextCursor == "older+/=&",
          model.loadError != nil, !model.searching else {
        fatalError("Page failure discarded results, retry cursor, or failure feedback")
    }

    let retry = Task { await model.loadMoreChats() }
    let retryRequest = try await ChatPageProtocol.next()
    retryRequest.reply(#"{"chats":[{"id":"b","title":"Beta","last_activity":200},{"id":"c","title":"Gamma","last_activity":100}],"next_cursor":null}"#)
    await retry.value
    await model.loadMoreChats()
    guard model.availableChats.map(\.id) == ["b", "c"], model.nextCursor == nil,
          model.loadError == nil, ChatPageProtocol.requests == 3,
          model.instructions["a"] == "Keep this draft.", model.queued == ["a"] else {
        fatalError("Retry, deduplication, exhaustion, or draft retention failed")
    }

    let refresh = Task { await model.searchChats() }
    let refreshRequest = try await ChatPageProtocol.next()
    refreshRequest.reply(#"{"chats":[{"id":"b","title":"Beta","last_activity":200}],"next_cursor":"stale-page"}"#)
    await refresh.value
    let stalePage = Task { await model.loadMoreChats() }
    let staleRequest = try await ChatPageProtocol.next()
    model.search = "Gamma"
    // A response arriving during the debounce must already be stale.
    staleRequest.reply(#"{"chats":[{"id":"wrong","title":"Gamma obsolete"}],"next_cursor":"wrong"}"#)
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
    betaRequest.reply(#"{"chats":[{"id":"b","title":"Beta","last_activity":200}],"next_cursor":"beta-page"}"#)
    await beta.value
    gammaRequest.reply(#"{"chats":[{"id":"c","title":"Gamma"}],"next_cursor":"gamma-page"}"#)
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
    betaMoreRequest.reply(#"{"chats":[],"next_cursor":"beta-last"}"#)
    await betaMore.value
    guard model.nextCursor == "beta-last", model.availableChats.map(\.id) == ["b"],
          !model.searching else {
        fatalError("Empty page was incorrectly treated as exhaustion")
    }

    model.search = ""
    let cleared = Task { await model.searchChats() }
    let clearedRequest = try await ChatPageProtocol.next()
    clearedRequest.reply(#"{"chats":[{"id":"a","title":"Alpha"},{"id":"c","title":"Gamma"}],"next_cursor":null}"#)
    await cleared.value
    guard model.availableChats.map(\.id) == ["c"], model.queued == ["a"],
          model.instructions["a"] == "Keep this draft." else { fatalError("Clearing search failed to restart pagination") }
    print("Chat pagination checks passed")
}

Task { @MainActor in
    do {
        try await testChatPagination()
        exit(0)
    } catch {
        fputs("\(error)\n", stderr)
        exit(1)
    }
}
RunLoop.main.run()
