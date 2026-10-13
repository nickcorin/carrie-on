import AppKit

enum Brand {
    /// A C flowing into a forward arrow, drawn as a template for either menu-bar appearance.
    static var trayIcon: NSImage {
        let image = NSImage(size: NSSize(width: 20, height: 20), flipped: true) { _ in
            NSColor.black.setStroke()
            let arc = NSBezierPath()
            arc.move(to: NSPoint(x: 14, y: 5))
            arc.curve(to: NSPoint(x: 8, y: 2.5), controlPoint1: NSPoint(x: 12.5, y: 3),
                      controlPoint2: NSPoint(x: 10.5, y: 2.5))
            arc.curve(to: NSPoint(x: 2.5, y: 9.5), controlPoint1: NSPoint(x: 4.5, y: 2.5),
                      controlPoint2: NSPoint(x: 2.5, y: 5.5))
            arc.curve(to: NSPoint(x: 8, y: 14), controlPoint1: NSPoint(x: 2.5, y: 12.5),
                      controlPoint2: NSPoint(x: 4.5, y: 14))
            arc.line(to: NSPoint(x: 16, y: 14))
            arc.lineWidth = 2.6
            arc.lineCapStyle = .round
            arc.lineJoinStyle = .round
            arc.stroke()

            let arrow = NSBezierPath()
            arrow.move(to: NSPoint(x: 13, y: 10.5))
            arrow.line(to: NSPoint(x: 16.5, y: 14))
            arrow.line(to: NSPoint(x: 13, y: 17.5))
            arrow.lineWidth = 2.6
            arrow.lineCapStyle = .round
            arrow.lineJoinStyle = .round
            arrow.stroke()
            return true
        }
        image.size = NSSize(width: 18, height: 18)
        image.isTemplate = true
        image.accessibilityDescription = "Carrie On"
        return image
    }
}
