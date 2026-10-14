// Render the real card views with isolated queue fixtures; never connect to Codex.
import SwiftUI

MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let expiry = Date().timeIntervalSince1970 + 120
    let states: [(String, String, Double?, String?)] = [
        ("Refine the checkout flow", "waiting", nil, nil),
        ("Finish chat pagination", "sending", expiry, nil),
        ("Update the migration tests", "needs_attention", nil,
         "Chat changed after enrollment; remove and re-enroll if still wanted")
    ]
    let instructions = [
        "Continue the checkout redesign and verify keyboard navigation.",
        "Finish the pagination tests and summarize any failures.",
        "Resume the migration after checking the failing tests."
    ]
    let content = VStack(spacing: 10) {
        ForEach(states.indices, id: \.self) { index in
            let fixture = states[index]
            let entry = PickerQueue.Entry(enrollmentID: "entry-\(index)", thread_id: "chat-\(index)",
                title: fixture.0, prompt: instructions[index], state: fixture.1,
                note: fixture.3, processing_until: fixture.2)
            ChatCard(chat: PickerChat(id: entry.id, title: entry.title, cwd: "/projects/carrie-on",
                                     last_activity: Date().timeIntervalSince1970 - 180),
                     activity: nil, content: .queued(entry, save: { _, _ in true }, remove: { true }),
                     onFlip: { _ in })
        }
    }
    .padding(20).frame(width: 440)
    .background(Color(nsColor: .windowBackgroundColor))
    .environment(\.colorScheme, CommandLine.arguments.contains("--dark") ? .dark : .light)
    let renderer = ImageRenderer(content: content)
    renderer.scale = 2
    guard let image = renderer.nsImage, let tiff = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:])
    else { fatalError("Could not render queue cards") }
    try! png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
}
