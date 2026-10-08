import AppKit
import KeryxKit
import SwiftUI

/// The ids of the app's windows, for `openWindow(id:)` from KeryxUI's views.
public enum KeryxWindowID {
    /// The main window: the sidebar and its sections.
    public static let main = "main"
    /// The setup wizard.
    public static let setup = "setup"
}

/// Whether the setup wizard opens by itself at launch: once per launch, only until setup
/// has once been seen through — after that a problem is the Overview's Setup Checklist's to
/// show, never a window that opens by itself — while the plan says Keryx is not ready, and
/// unless the owner has said not to.
public enum SetupLaunch {
    /// The app's own defaults key for Welcome's "Don't show this again".
    public static let hiddenKey = "setupAssistantHiddenAtLaunch"
    /// Set the first time setup is seen to be through: Checking reaches You're Ready (or
    /// Nearly Ready), or a launch finds the plan ready (or all but what was left for later).
    /// Never by closing the wizard: "Don't show this again" is the way to turn it off.
    public static let completedKey = "setupCompletedOnce"

    /// Open the wizard now: setup has never been seen through, a plan has been read and is
    /// not ready, the owner has not turned it off, and it has not opened by itself already
    /// this launch. No plan (Keryx not found, or the plan failed to load) opens nothing: the
    /// main window says why.
    public static func shouldOpen(plan: SetupPlan?, hidden: Bool, alreadyOpened: Bool,
                                  completedOnce: Bool = false) -> Bool {
        guard let plan, !hidden, !alreadyOpened, !completedOnce else { return false }
        // Everything done but what was left for later is not a reason to ask again.
        return !plan.ready && !plan.nearlyReady
    }

    /// The launch's decision, once the plan is read: a plan that is ready (or nearly) is
    /// recorded as setup seen through — an owner set up from a terminal, or by an earlier
    /// app — so a later problem is the checklist's; otherwise `shouldOpen`.
    public static func atLaunch(plan: SetupPlan?, hidden: Bool, alreadyOpened: Bool,
                                defaults: UserDefaults = .standard) -> Bool {
        if let plan, plan.ready || plan.nearlyReady { markCompleted(in: defaults) }
        return shouldOpen(plan: plan, hidden: hidden, alreadyOpened: alreadyOpened,
                          completedOnce: completedOnce(in: defaults))
    }

    /// Whether the owner has said not to open it at launch, in `defaults`.
    public static func isHidden(in defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: hiddenKey)
    }

    /// Whether setup has been seen through once, in `defaults`.
    public static func completedOnce(in defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: completedKey)
    }

    /// Records that setup has been seen through (or left from Checking).
    public static func markCompleted(in defaults: UserDefaults = .standard) {
        defaults.set(true, forKey: completedKey)
    }

    /// Opens the wizard from `entry`, in the mode that entry means (`SetupEntry.mode`), with
    /// whether setup has been seen through read from `defaults`.
    @MainActor public static func open(_ entry: SetupEntry, model: AppModel, defaults: UserDefaults = .standard) {
        model.openSetupWizard(entry.mode(completedOnce: completedOnce(in: defaults)))
    }
}

/// Where the wizard is opened from.
public enum SetupEntry: Sendable, CaseIterable {
    /// By itself, once at launch.
    case launch
    /// The menu's "Run Setup Wizard…".
    case menu
    /// The Overview's Setup Checklist, "Set Up Keryx…".
    case checklist

    /// The mode it opens in: the menu reviews every frame; launch walks only what is left;
    /// the Overview's "Set Up Keryx…" walks what is left only until setup has once been seen
    /// through (`completedOnce`) and reviews after — a review starts nothing by itself, so an
    /// owner set up long ago is never charged for the agent's (paid) test by opening it.
    public func mode(completedOnce: Bool) -> SetupWizardMode {
        switch self {
        case .menu: .review
        case .launch: .firstRun
        case .checklist: completedOnce ? .review : .firstRun
        }
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
