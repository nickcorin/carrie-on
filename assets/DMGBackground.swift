import AppKit

let scale = Int(CommandLine.arguments[2])!
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 640 * scale, pixelsHigh: 360 * scale,
    bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
    colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
bitmap.size = NSSize(width: 640, height: 360)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSColor(red: 0.96, green: 0.97, blue: 0.99, alpha: 1).setFill()
NSRect(x: 0, y: 0, width: 640, height: 360).fill()

func label(_ text: String, y: CGFloat, size: CGFloat, weight: NSFont.Weight, color: NSColor) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = .center
    (text as NSString).draw(in: NSRect(x: 24, y: y, width: 592, height: 40), withAttributes: [
        .font: NSFont.systemFont(ofSize: size, weight: weight),
        .foregroundColor: color, .paragraphStyle: paragraph,
    ])
}
label("Install Carrie On", y: 287, size: 26, weight: .semibold,
      color: NSColor(red: 0.12, green: 0.17, blue: 0.29, alpha: 1))
label("Drag Carrie On to Applications", y: 49, size: 17, weight: .medium,
      color: NSColor(red: 0.25, green: 0.31, blue: 0.43, alpha: 1))
label("Then open it from your Applications folder.", y: 20, size: 13, weight: .regular,
      color: NSColor(red: 0.43, green: 0.48, blue: 0.58, alpha: 1))
let arrow = NSBezierPath()
arrow.lineWidth = 3
arrow.lineCapStyle = .round
arrow.lineJoinStyle = .round
arrow.move(to: NSPoint(x: 294, y: 190))
arrow.line(to: NSPoint(x: 346, y: 190))
arrow.move(to: NSPoint(x: 335, y: 201))
arrow.line(to: NSPoint(x: 346, y: 190))
arrow.line(to: NSPoint(x: 335, y: 179))
NSColor(red: 0.52, green: 0.59, blue: 0.75, alpha: 1).setStroke()
arrow.stroke()
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
