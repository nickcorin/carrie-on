// Exercise the real picker with a private, deterministic catalog and no service lifecycle.
import SwiftUI

@MainActor
private func scrollViews(in view: NSView) -> [NSScrollView] {
    ([view as? NSScrollView].compactMap { $0 }) + view.subviews.flatMap(scrollViews)
}

@MainActor
private func accessibilityNodes(in root: NSObject) -> [NSObject] {
    var pending = [root]
    var visited: Set<ObjectIdentifier> = []
    var nodes: [NSObject] = []
    while let node = pending.popLast(), nodes.count < 2_000 {
        guard visited.insert(ObjectIdentifier(node)).inserted else { continue }
        nodes.append(node)
        let children = (node as? NSAccessibilityProtocol)?.accessibilityChildren() as? [NSObject]
            ?? node.accessibilityAttributeValue(.children) as? [NSObject] ?? []
        pending.append(contentsOf: children.reversed())
    }
    return nodes
}

@MainActor
private func verifyScrolling(window: NSWindow, model: ChatPickerModel) async {
    func require(_ condition: Bool, _ message: String) {
        guard condition else { print("FAIL: \(message)"); fflush(stdout); exit(1) }
    }
    func checkpoint(_ message: String) { print(message); fflush(stdout) }
    func settle() async { try? await Task.sleep(nanoseconds: 500_000_000) }
    func snapshots(_ name: String) async {
        guard let index = CommandLine.arguments.firstIndex(of: "--snapshots"),
              CommandLine.arguments.indices.contains(index + 1) else { return }
        let directory = URL(fileURLWithPath: CommandLine.arguments[index + 1])
        try! FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        for (theme, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
            NSApplication.shared.appearance = NSAppearance(named: appearance)
            await settle()
            let view = window.contentView!
            let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds)!
            view.cacheDisplay(in: view.bounds, to: bitmap)
            try! bitmap.representation(using: .png, properties: [:])!
                .write(to: directory.appendingPathComponent("\(name)-\(theme).png"))
        }
    }
    func perform(_ action: String, in root: NSObject) async {
        // These public legacy methods expose the same action used by assistive technologies;
        // AppKit has no modern protocol method for the scroll-to-top/bottom actions.
        let actionName = NSAccessibility.Action(rawValue: action)
        let node = accessibilityNodes(in: root).first {
            $0.accessibilityActionNames().contains(actionName)
        }
        require(node != nil, "\(action) must be accessible")
        print("Performing \(action) with \(model.availableChats.count) loaded chats")
        fflush(stdout)
        node!.accessibilityPerformAction(actionName)
        await settle()
    }
    func verifyVirtualBoundaryActions(_ document: NSView) async {
        // AppKit scroll actions require the external AX bridge; direct legacy calls bypass it.
        // Virtual SwiftUI actions use this entry point, so exercise any exposed endpoints.
        guard !(document is NSTableView) else { return }
        for action in ["AXScrollToTop", "AXScrollToBottom"] {
            if accessibilityNodes(in: window).contains(where: {
                $0.accessibilityActionNames().contains(NSAccessibility.Action(rawValue: action))
            }) { await perform(action, in: window) }
        }
    }
    await settle()
    await snapshots("picker")
    for search in (CommandLine.arguments.contains("--queue-only") ? [] : ["", "Fixture"]) {
        checkpoint("Checking chats after search: \(search.isEmpty ? "empty" : search)")
        if !search.isEmpty {
            model.search = search
            await settle()
        }
        require(model.availableChats.count == 25, "opening/searching must load only the first page")
        guard let scroll = scrollViews(in: window.contentView!).first,
              let document = scroll.documentView else {
            require(false, "available chats must have a native scroll view"); return
        }
        for expectedCount in [50, 75] {
            for _ in 0..<8 where model.availableChats.count < expectedCount {
                let y = max(0, document.bounds.height - scroll.contentView.bounds.height)
                scroll.contentView.scroll(to: NSPoint(x: 0, y: y))
                scroll.reflectScrolledClipView(scroll.contentView)
                await settle()
            }
            require(model.availableChats.count == expectedCount,
                    "reaching the footer must load the next page (expected \(expectedCount), got \(model.availableChats.count), viewport \(scroll.contentView.bounds), document \(document.bounds))")
            checkpoint("Loaded \(expectedCount) chats")
        }
        require(scroll.contentView.bounds.minY > 0, "paging must move the viewport")
        await verifyVirtualBoundaryActions(document)
        checkpoint("Checking beginning/end navigation")
        document.scrollToBeginningOfDocument(nil)
        await settle()
        require(abs(scroll.contentView.bounds.minY) < 1, "scroll-to-top must reveal the first chat")
        require(model.availableChats.count == 75, "scroll-to-top must not drain remaining pages")
        for _ in 0..<8 where model.availableChats.count < 100 {
            document.scrollToEndOfDocument(nil)
            await settle()
        }
        require(scroll.contentView.bounds.minY > 0, "scroll-to-bottom must move down")
        require(model.availableChats.count == 100, "scroll-to-bottom must load the next page")
        document.scrollToBeginningOfDocument(nil)
        await settle()
        require(abs(scroll.contentView.bounds.minY) < 1, "scroll-to-top must work after pagination")
    }
    model.search = ""
    checkpoint("Checking queued overflow")
    await settle()
    model.queuedEntries = (1...75).map { index in
        PickerQueue.Entry(enrollmentID: String(format: "10000000-0000-4000-8000-%012d", index),
                          thread_id: String(format: "20000000-0000-4000-8000-%012d", index),
                          title: "Queued fixture \(index)", prompt: "Synthetic instruction")
    }
    await settle()
    guard let queued = scrollViews(in: window.contentView!).first,
          let document = queued.documentView else {
        require(false, "queued chats must have a native scroll view"); return
    }
    let bottom = max(0, document.bounds.height - queued.contentView.bounds.height)
    require(bottom > 0, "the queued fixture must overflow")
    queued.contentView.scroll(to: NSPoint(x: 0, y: bottom))
    queued.reflectScrolledClipView(queued.contentView)
    await settle()
    document.scrollToBeginningOfDocument(nil)
    await settle()
    require(abs(queued.contentView.bounds.minY) < 1, "queued scroll-to-top must reveal the first item")
    await verifyVirtualBoundaryActions(document)
    document.scrollToEndOfDocument(nil)
    await settle()
    require(queued.contentView.bounds.minY > 0, "queued end navigation must move down")
    document.scrollToBeginningOfDocument(nil)
    await settle()
    await snapshots("queued-picker")
    require(model.queuedEntries.count == 75, "scrolling must preserve all queue items")
    print("PASS: lazy pagination, search, and beginning/end navigation in both lists")
    fflush(stdout)
    NSApplication.shared.terminate(nil)
}

final class ScrollFixtureProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        guard request.httpMethod == "GET" else { fatalError("The scroll fixture must not submit queue mutations") }
        let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
        let search = components.queryItems?.first(where: { $0.name == "search" })?.value ?? ""
        let offset = Int(components.queryItems?.first(where: { $0.name == "cursor" })?.value ?? "0") ?? 0
        var body: [String: Any] = ["items": [], "next_cursor": NSNull()]
        if components.path == "/api/v1/chats" {
            let chats: [[String: Any]] = (1...150).map { index in
                ["id": String(format: "00000000-0000-4000-8000-%012d", index),
                 "title": String(format: "Fixture chat %03d", index), "cwd": "/tmp/scroll-fixture",
                 "last_activity": 1_900_000_000 - index]
            }.filter { search.isEmpty || ($0["title"] as! String).localizedStandardContains(search) }
            body["items"] = Array(chats.dropFirst(offset).prefix(25))
            body["next_cursor"] = offset + 25 < chats.count ? String(offset + 25) : NSNull()
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: body))
        client?.urlProtocolDidFinishLoading(self)
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    app.accessibilitySetValue(true, forAttribute: NSAccessibility.Attribute(rawValue: "AXEnhancedUserInterface"))
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [ScrollFixtureProtocol.self]
    let session = URLSession(configuration: configuration)
    let model = ChatPickerModel(baseURL: URL(string: "http://scroll.fixture")!, session: session,
                                recoveryDefaults: nil, didQueue: {})
    model.connection = .connected
    model.ready = true
    let lifecycle = AppLifecycle(isStandalone: false, isDevelopment: false)
    let content = ChatPickerView(model: model, lifecycle: lifecycle)
        .background(Color(nsColor: .windowBackgroundColor))
    let window = NSWindow(contentRect: NSRect(x: 100, y: 100, width: 440, height: 620),
                          styleMask: [.titled, .closable], backing: .buffered, defer: false)
    window.title = "Carrie On Scroll Fixture"
    window.contentView = NSHostingView(rootView: content)
    window.makeKeyAndOrderFront(nil)
    app.activate(ignoringOtherApps: true)
    if CommandLine.arguments.contains("--verify-scrolling") {
        Task { await verifyScrolling(window: window, model: model) }
    }
    withExtendedLifetime((model, lifecycle, session, window)) { app.run() }
}
