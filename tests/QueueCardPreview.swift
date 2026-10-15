// Render the real card views with isolated queue fixtures; never connect to Codex.
import SwiftUI

MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let dark = CommandLine.arguments.contains("--dark")
    app.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
    let expiry = Date().timeIntervalSince1970 + 120
    let states: [(String, String, Double?, String?)] = [
        ("Refine the checkout flow", "queued", nil, nil),
        ("Finish chat pagination", "queued", expiry, nil),
        ("Update the migration tests", "changed", nil,
         "Chat changed after enrollment; remove and re-enroll if still wanted")
    ]
    let instructions = [
        "Continue the checkout redesign and verify keyboard navigation.",
        "Finish the pagination tests and summarize any failures.",
        "Resume the migration after checking the failing tests."
    ]
    let entries = states.indices.map { index in
        let fixture = states[index]
        var entry = PickerQueue.Entry(enrollmentID: "entry-\(index)", thread_id: "chat-\(index)",
                                      title: fixture.0, prompt: instructions[index])
        entry.state = fixture.1
        entry.note = fixture.3
        entry.attempted_at = fixture.2 == nil ? nil : expiry - 120
        entry.processing_until = fixture.2
        return entry
    }
    let content = VStack(spacing: 10) {
        ForEach(entries) { entry in
            ChatCard(chat: PickerChat(id: entry.thread_id, title: entry.title, cwd: "/projects/carrie-on",
                                     last_activity: Date().timeIntervalSince1970 - 180),
                     activity: nil, content: .queued(entry, save: { _, _ in true }, remove: { true }),
                     onFlip: { _ in })
        }
    }
    .padding(20).frame(width: 440)
    .background(Color(nsColor: .windowBackgroundColor))
    .environment(\.colorScheme, dark ? .dark : .light)
    let hosting = NSHostingView(rootView: content)
    hosting.frame = CGRect(origin: .zero, size: hosting.fittingSize)
    let window = NSWindow(contentRect: hosting.frame, styleMask: .borderless, backing: .buffered, defer: false)
    window.contentView = hosting
    hosting.layoutSubtreeIfNeeded()
    guard let bitmap = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds)
    else { fatalError("Could not create native snapshot") }
    hosting.cacheDisplay(in: hosting.bounds, to: bitmap)
    guard let png = bitmap.representation(using: .png, properties: [:])
    else { fatalError("Could not render queue cards") }
    try! png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
}
