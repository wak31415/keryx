import AppKit
import KeryxKit

/// The menu-bar images: the mark's three bars as an 18 × 18 pt template image, so the menu
/// bar tints them for its own appearance. Drawn with `NSBezierPath` at draw time, so they are
/// sharp at every scale.
///
/// - `.plain`: the three bars.
/// - `.dot`: the bars moved left, and a 5 pt dot top right — something to look at.
/// - `.slashed`: the bars at half strength, struck through — Keryx is not running.
///
/// Where a dot or a slash crosses the bars, the bars are cut back by a hairline gap (by
/// clipping, never by erasing, so nothing beneath the image is ever cleared).
public enum MenuBarIcon {
    @MainActor public static func image(_ symbol: MenuSymbol) -> NSImage {
        let image = NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in
            draw(symbol)
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "Keryx"
        return image
    }

    static let side: CGFloat = 18
    /// Bars 2.5 pt wide (5 px at 2x) at the mark's proportions: 5 pt apart, the centre one
    /// about 16 pt tall.
    private static let scale: CGFloat = 2.5 / KeryxMark.strokeWidth
    /// Where the centre bar sits. A quarter point off centre puts every bar's edges on whole
    /// pixels at 2x, so they are crisp in the menu bar rather than blurred over a half pixel.
    private static let centre: CGFloat = 9.25
    /// Two and a half points further left with a dot (still on whole pixels), which keeps the
    /// right bar clear of the dot's gap.
    private static let centreWithDot: CGFloat = 6.75
    private static let dotDiameter: CGFloat = 5
    private static let gap: CGFloat = 1
    private static let slashWidth: CGFloat = 1.75

    private static func draw(_ symbol: MenuSymbol) {
        let black = NSColor.black
        switch symbol {
        case .plain:
            bars(centreX: centre, colour: black)
        case .dot:
            let dot = NSRect(x: side - dotDiameter, y: side - dotDiameter, width: dotDiameter, height: dotDiameter)
            NSGraphicsContext.saveGraphicsState()
            clip(excluding: NSBezierPath(ovalIn: dot.insetBy(dx: -gap, dy: -gap)))
            bars(centreX: centreWithDot, colour: black)
            NSGraphicsContext.restoreGraphicsState()
            black.setFill()
            NSBezierPath(ovalIn: dot).fill()
        case .slashed:
            let from = NSPoint(x: 2.5, y: side - 2.5), to = NSPoint(x: side - 2.5, y: 2.5)
            NSGraphicsContext.saveGraphicsState()
            clip(excluding: band(from: from, to: to, width: slashWidth + 2 * gap))
            bars(centreX: centre, colour: black.withAlphaComponent(0.5))
            NSGraphicsContext.restoreGraphicsState()
            let slash = NSBezierPath()
            slash.move(to: from)
            slash.line(to: to)
            slash.lineWidth = slashWidth
            slash.lineCapStyle = .round
            black.setStroke()
            slash.stroke()
        }
    }

    /// The mark's bars, centred vertically and on `centreX`, from `KeryxMark`'s geometry.
    private static func bars(centreX: CGFloat, colour: NSColor) {
        colour.setStroke()
        for bar in KeryxMark.bars(time: nil) {
            let path = NSBezierPath()
            let x = centreX + (bar.x - KeryxMark.box / 2) * scale
            path.move(to: NSPoint(x: x, y: side / 2 + (bar.top - KeryxMark.box / 2) * scale))
            path.line(to: NSPoint(x: x, y: side / 2 + (bar.bottom - KeryxMark.box / 2) * scale))
            path.lineWidth = KeryxMark.strokeWidth * scale
            path.lineCapStyle = .round
            path.stroke()
        }
    }

    /// Clips drawing to the whole image except `shape`.
    private static func clip(excluding shape: NSBezierPath) {
        let outside = NSBezierPath(rect: NSRect(x: 0, y: 0, width: side, height: side))
        outside.append(shape)
        outside.windingRule = .evenOdd
        outside.addClip()
    }

    /// The outline of a stroke from `from` to `to`, `width` wide with round ends.
    private static func band(from: NSPoint, to: NSPoint, width: CGFloat) -> NSBezierPath {
        let dx = to.x - from.x, dy = to.y - from.y
        let length = (dx * dx + dy * dy).squareRoot()
        let rect = NSRect(x: -width / 2, y: -width / 2, width: length + width, height: width)
        let path = NSBezierPath(roundedRect: rect, xRadius: width / 2, yRadius: width / 2)
        var transform = AffineTransform(translationByX: from.x, byY: from.y)
        transform.rotate(byRadians: atan2(dy, dx))
        path.transform(using: transform)
        return path
    }
}
