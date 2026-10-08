import AppKit
import SwiftUI

/// Draws a view to pixels, for the snapshot tool and the render tests.
///
/// Two ways, because each sees something the other cannot:
/// - `render` draws offscreen in a *borderless* window. It needs no permission, so the tests
///   use it; but it has no title bar, and a titled window's Liquid Glass chrome draws blank
///   this way, which is why the window is borderless.
/// - `capture` shows a real *titled* window on screen and has `screencapture` photograph that
///   one window by its id — the actual glass sidebar and toolbar. It needs Screen Recording,
///   and it never photographs the screen or any other app's window.
@MainActor public enum Snapshot {
    public enum Failure: Error, CustomStringConvertible {
        case capture(status: Int32, file: String)

        public var description: String {
            switch self {
            case let .capture(status, file): "screencapture exited \(status) without writing \(file)"
            }
        }
    }

    /// The view at `size` points on a window background, in light or dark, at the screen's
    /// backing scale.
    public static func render(_ view: AnyView, size: CGSize, dark: Bool) -> NSBitmapImageRep {
        let window = NSWindow(contentRect: NSRect(origin: CGPoint(x: -10_000, y: 0), size: size),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = appearance(dark: dark)
        let host = hostingView(view, size: size, fixed: true)
        window.contentView = host
        window.orderFrontRegardless()
        settle(seconds: 0.6)
        host.layoutSubtreeIfNeeded()
        let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
        host.cacheDisplay(in: host.bounds, to: rep)
        window.close()
        return rep
    }

    /// The view in a real titled window, photographed by window id into `url` (a PNG).
    public static func capture(_ view: AnyView, size: CGSize, dark: Bool, to url: URL, title: String = "") throws {
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.title = title
        window.appearance = appearance(dark: dark)
        window.contentView = hostingView(view, size: size, fixed: false)
        window.center()
        // The key window of the active app, so the primary button and the traffic lights are
        // drawn as the owner sees them, not dimmed as an inactive window's are.
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        settle(seconds: 1.2)
        defer { window.close() }

        try? FileManager.default.removeItem(at: url)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        // -x: no sound; -o: no shadow; -l: this window and nothing else.
        process.arguments = ["-x", "-o", "-l\(window.windowNumber)", url.path]
        try process.run()
        process.waitUntilExit()
        guard process.terminationStatus == 0, FileManager.default.fileExists(atPath: url.path) else {
            throw Failure.capture(status: process.terminationStatus, file: url.path)
        }
    }

    /// `rep` encoded as PNG.
    public static func png(_ rep: NSBitmapImageRep) -> Data? {
        rep.representation(using: .png, properties: [:])
    }

    private static func appearance(dark: Bool) -> NSAppearance? {
        NSAppearance(named: dark ? .darkAqua : .aqua)
    }

    /// `fixed`: the view is exactly `size`. Otherwise it fills what the window leaves it —
    /// in a titled window that is `size` less the title bar, and a fixed frame would be
    /// clipped at the top and bottom.
    private static func hostingView(_ view: AnyView, size: CGSize, fixed: Bool) -> NSHostingView<AnyView> {
        let framed = fixed
            ? AnyView(view.frame(width: size.width, height: size.height))
            : AnyView(view.frame(maxWidth: .infinity, maxHeight: .infinity))
        // Drawn as the key window's controls are (a prominent primary in the accent colour),
        // since the harness is seldom the active app and macOS will not make it so.
        let root = AnyView(framed.background(Color(nsColor: .windowBackgroundColor))
            .environment(\.controlActiveState, .key))
        let host = NSHostingView(rootView: root)
        // The window is sized here; the view must not resize it to its own ideal size.
        host.sizingOptions = []
        host.frame = NSRect(origin: .zero, size: size)
        return host
    }

    /// Lets lists and tables populate and animations reach their first frame.
    private static func settle(seconds: TimeInterval) {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: seconds))
    }
}
