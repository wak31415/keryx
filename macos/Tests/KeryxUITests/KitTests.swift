import AppKit
import Foundation
import Testing
@testable import KeryxKit
@testable import KeryxUI

@Suite struct MarkGeometryTests {
    @Test func idleBarsAreTheSVGsBars() {
        // docs/assets/keryx-mark.svg: M 20 24 V 40 (a), M 44 24 V 40 (c), M 32 16 V 48 (b).
        let bars = KeryxMark.bars(time: nil)
        #expect(bars.map(\.x) == [20, 44, 32])
        #expect(bars.map(\.top) == [24, 24, 16])
        #expect(bars.map(\.bottom) == [40, 40, 48])
        #expect(bars.map(\.tint) == [.a, .c, .b])
        #expect(KeryxMark.strokeWidth == 6)
    }

    @Test func paletteIsTheSVGsPalette() {
        #expect([KeryxMark.Tint.a, .b, .c].map { KeryxMark.hex($0, dark: false) } == [0xF97316, 0xDB2777, 0x7C3AED])
        #expect([KeryxMark.Tint.a, .b, .c].map { KeryxMark.hex($0, dark: true) } == [0xFB923C, 0xF472B6, 0xA78BFA])
    }

    @Test func workingBarsStayInsideTheBoxAndCentred() {
        for step in 0..<50 {
            let bars = KeryxMark.bars(time: Double(step) * 0.137)
            for bar in bars {
                #expect(bar.top >= 16 && bar.bottom <= 48 && bar.top <= bar.bottom)
                #expect(abs((bar.top + bar.bottom) / 2 - 32) < 0.0001)
            }
        }
    }

    @Test func workingBarsMove() {
        let a = KeryxMark.bars(time: 0), b = KeryxMark.bars(time: 0.7)
        #expect(a.map(\.top) != b.map(\.top))
    }
}

@MainActor @Suite struct MenuBarIconTests {
    @Test func everySymbolIsAn18PointTemplate() {
        for symbol in MenuSymbol.allCases {
            let image = MenuBarIcon.image(symbol)
            #expect(image.isTemplate)
            #expect(image.size == NSSize(width: 18, height: 18))
        }
    }

    @Test func symbolsDrawDifferently() {
        let pngs = MenuSymbol.allCases.map { symbol -> Data in
            let image = MenuBarIcon.image(symbol)
            let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 36, pixelsHigh: 36, bitsPerSample: 8,
                                       samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                       colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            rep.size = image.size
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            image.draw(in: NSRect(origin: .zero, size: image.size))
            NSGraphicsContext.restoreGraphicsState()
            return rep.representation(using: .png, properties: [:])!
        }
        #expect(Set(pngs).count == 3)
    }
}

@Suite struct FormattingTests {
    let en = Locale(identifier: "en_US")

    @Test func gigabytesAreDecimal() {
        // The CLI reports sizes in decimal gigabytes (bytes / 1e9).
        #expect(Formatting.gigabytes(36) == "36 GB")
        #expect(Formatting.gigabytes(412) == "412 GB")
        #expect(Formatting.bytes(2_000_000_000_000) == "2 TB")
    }

    @Test func costIsDollarsAndCents() {
        #expect(Formatting.cost(1.234, locale: en) == "$1.23")
        #expect(Formatting.cost(37.42, locale: en) == "$37.42")
        #expect(Formatting.cost(0, locale: en) == "$0.00")
    }

    @Test func relativeDatesReadAsSpeech() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        #expect(Formatting.relative(now.addingTimeInterval(-7200), now: now, locale: en) == "2 hours ago")
        #expect(Formatting.relative(now.addingTimeInterval(-86_400 * 3), now: now, locale: en) == "3 days ago")
    }
}
