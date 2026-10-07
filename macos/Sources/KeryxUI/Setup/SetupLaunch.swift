import AppKit
import KeryxKit
import SwiftUI

/// The ids of the app's windows, for `openWindow(id:)` from KeryxUI's views.
public enum KeryxWindowID {
    /// The main window: the sidebar and its sections.
    public static let main = "main"
    /// The Setup Assistant.
    public static let setup = "setup"
}

/// Whether the Setup Assistant opens by itself at launch: once per launch, while the plan
/// says Keryx is not ready for a first call, unless the owner has said not to.
public enum SetupLaunch {
    /// The app's own defaults key for "Don't show at launch".
    public static let hiddenKey = "setupAssistantHiddenAtLaunch"

    /// Open the assistant now: a plan has been read and is not ready, the owner has not
    /// turned it off, and it has not opened by itself already this launch. No plan (Keryx not
    /// found, or the plan failed to load) opens nothing: the main window says why.
    public static func shouldOpen(plan: SetupPlan?, hidden: Bool, alreadyOpened: Bool) -> Bool {
        guard let plan, !hidden, !alreadyOpened else { return false }
        return !plan.ready
    }

    /// Whether the owner has said not to open it at launch, in `defaults`.
    public static func isHidden(in defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: hiddenKey)
    }
}

extension View {
    /// Calls `action` each time the window holding this view becomes the key window.
    func onWindowBecomingKey(_ action: @escaping @MainActor () -> Void) -> some View {
        background(WindowKeyObserver(action: action))
    }
}

/// Finds the view's window and watches it become key.
private struct WindowKeyObserver: NSViewRepresentable {
    let action: @MainActor () -> Void

    func makeNSView(context: Context) -> ObservingView {
        let view = ObservingView()
        view.action = action
        return view
    }

    func updateNSView(_ view: ObservingView, context: Context) {
        view.action = action
    }

    /// Observes with a selector, which the notification centre lets go of by itself when the
    /// view goes.
    final class ObservingView: NSView {
        var action: (@MainActor () -> Void)?

        override func viewWillMove(toWindow newWindow: NSWindow?) {
            super.viewWillMove(toWindow: newWindow)
            NotificationCenter.default.removeObserver(self, name: NSWindow.didBecomeKeyNotification, object: nil)
            guard let newWindow else { return }
            NotificationCenter.default.addObserver(self, selector: #selector(windowDidBecomeKey(_:)),
                                                   name: NSWindow.didBecomeKeyNotification, object: newWindow)
        }

        @objc private func windowDidBecomeKey(_ notification: Notification) {
            action?()
        }
    }
}
