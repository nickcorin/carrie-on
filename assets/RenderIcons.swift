import AppKit

@main
struct RenderIcons {
    static func verifyReference(in folder: URL) throws {
        let width = 380
        let height = 315
        let masks = try ["mark-reference-mask.png", "mark-rendered.png"].map { name in
            let data = try Data(contentsOf: folder.appendingPathComponent(name))
            guard let image = NSBitmapImageRep(data: data),
                  image.pixelsWide == width, image.pixelsHigh == height else {
                throw NSError(domain: "IconBuild", code: 1, userInfo: [NSLocalizedDescriptionKey:
                    "Expected a 380 × 315 silhouette: \(name)"])
            }
            return (0..<(width * height)).map { index in
                image.colorAt(x: index % width, y: index / width)!.usingColorSpace(.sRGB)!.redComponent < 0.5
            }
        }
        let reference = masks[0]
        let rendered = masks[1]
        var intersection = 0
        var union = 0
        var differing = 0
        var distant = 0
        for index in reference.indices {
            if reference[index] && rendered[index] { intersection += 1 }
            if reference[index] || rendered[index] { union += 1 }
            guard reference[index] != rendered[index] else { continue }
            differing += 1
            let other = reference[index] ? rendered : reference
            let x = index % width
            let y = index / width
            let nearby = (-1...1).contains { dy in
                (-1...1).contains { dx in
                    let nx = x + dx
                    let ny = y + dy
                    return nx >= 0 && nx < width && ny >= 0 && ny < height && other[ny * width + nx]
                }
            }
            if !nearby { distant += 1 }
        }
        let overlap = Double(intersection) / Double(union)
        guard overlap >= 0.99, distant == 0 else {
            throw NSError(domain: "IconBuild", code: 2, userInfo: [NSLocalizedDescriptionKey:
                "Vector silhouette drift: overlap \(overlap), \(distant) pixels beyond the 1px edge tolerance"])
        }
        let report: [String: Any] = ["reference_width": width, "reference_height": height,
            "silhouette_overlap": overlap, "differing_edge_pixels": differing,
            "pixels_beyond_one_pixel_tolerance": distant]
        try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
            .write(to: folder.appendingPathComponent("trace-verification.json"))
        print("Silhouette verified: \(String(format: "%.2f", overlap * 100))% overlap; no edge drift beyond 1px.")
    }

    static func write(_ name: String, width: Int, height: Int, to folder: URL,
                      drawing: @escaping (NSRect) -> Bool) throws {
        let size = NSSize(width: width, height: height)
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height,
                                      bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                      isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0,
                                      bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        NSImage(size: size, flipped: true, drawingHandler: drawing)
            .draw(in: NSRect(origin: .zero, size: size))
        NSGraphicsContext.restoreGraphicsState()
        try bitmap.representation(using: .png, properties: [:])!
            .write(to: folder.appendingPathComponent(name))
    }

    static func main() throws {
        let folder = URL(fileURLWithPath: CommandLine.arguments[1])
        try write("app-icon.png", width: 1024, height: 1024, to: folder) { _ in
            NSColor(srgbRed: 53 / 255, green: 77 / 255, blue: 232 / 255, alpha: 1).setFill()
            NSBezierPath(roundedRect: NSRect(x: 72, y: 72, width: 880, height: 880),
                         xRadius: 196, yRadius: 196).fill()
            NSColor(srgbRed: 244 / 255, green: 244 / 255, blue: 1, alpha: 1).setFill()
            Brand.markPath(in: NSRect(x: 195, y: 195, width: 634, height: 634)).fill()
            return true
        }
        try write("mark-rendered.png", width: 380, height: 315, to: folder) { bounds in
            NSColor.white.setFill()
            bounds.fill()
            NSColor.black.setFill()
            Brand.markPath(in: bounds).fill()
            return true
        }
        try verifyReference(in: folder)
        for size in [18, 36] {
            try write("tray-\(size).png", width: size, height: size, to: folder) { bounds in
                NSColor.black.setFill()
                Brand.markPath(in: bounds).fill()
                return true
            }
        }
    }
}
