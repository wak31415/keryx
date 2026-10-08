import AppKit
import KeryxKit
import KeryxUI
import SwiftUI

/// Keryx for Mac: a menu bar item that is always there, a window opened from it, and the
/// app's own Settings. A regular app, in the Dock for as long as it runs: closing its window
/// leaves it running, and Keryx answering calls; Quit stops both (`AppLifecycle`).
@main
struct KeryxApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        Window("Keryx", id: KeryxWindow.main) {
            MainWindowContent(delegate: delegate)
                .environment(delegate.model)
                .environment(\.preferences, delegate.preferences)
        }
        .defaultSize(width: 1100, height: 720)
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(before: .toolbar) {
                RefreshCommand(model: delegate.model)
            }
        }

        // The Setup Assistant: opened from the menu, from the Overview, and by itself at
        // launch while setup is not finished (`AppDelegate.openSetupIfNeeded`).
        Window("Set Up Keryx", id: KeryxWindow.setup) {
            SetupAssistantView()
                .environment(delegate.model)
                .environment(\.preferences, delegate.preferences)
                .onAppear { delegate.windowDidAppear(.setup) }
                .onDisappear { delegate.windowDidDisappear(.setup) }
        }
        .defaultSize(width: 820, height: 600)
        .windowResizability(.contentSize)

        MenuBarExtra {
            MenuBarMenu(delegate: delegate)
                .environment(delegate.model)
        } label: {
            MenuBarLabel(delegate: delegate)
        }
        .menuBarExtraStyle(.menu)

        Settings {
            AppSettingsView(loginItem: LoginItem.control)
                .environment(delegate.model)
                .environment(\.preferences, delegate.preferences)
                .onAppear { delegate.windowDidAppear(.settings) }
                .onDisappear { delegate.windowDidDisappear(.settings) }
        }
    }
}

enum KeryxWindow {
    static let main = KeryxWindowID.main
    /// The Setup Assistant's window.
    static let setup = KeryxWindowID.setup
}

/// The window's content. SwiftUI opens it at launch; at a login launch it closes again at
/// once, leaving the app in the Dock and the menu bar.
private struct MainWindowContent: View {
    let delegate: AppDelegate
    @Environment(\.dismissWindow) private var dismissWindow

    var body: some View {
        RootView()
            .task { await delegate.start() }
            .onAppear {
                if delegate.windowAppearsAtLoginLaunch() {
                    dismissWindow(id: KeryxWindow.main)
                    return
                }
                delegate.windowDidAppear(.main)
            }
            .onDisappear {
                delegate.windowDidDisappear(.main)
            }
    }
}

/// The menu bar menu, and what its items do that only the app can.
private struct MenuBarMenu: View {
    let delegate: AppDelegate
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        MenuContent(actions: MenuActions(
            openWindow: { open() },
            openSession: { id in
                delegate.model.selection = .sessions
                delegate.model.selectedTaskID = id
                open()
            },
            quit: { NSApp.terminate(nil) },
            openSetup: { open(KeryxWindow.setup) }))
    }

    private func open(_ id: String = KeryxWindow.main) {
        openWindow(id: id)
        NSApp.activate()
    }
}

/// The menu bar image: the mark's bars, with a dot or a slash for what is worth knowing.
/// Alive as long as the app is, so it is what hands the delegate a way to open the window.
private struct MenuBarLabel: View {
    let delegate: AppDelegate
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let state = delegate.model.menuState
        Image(nsImage: MenuBarIcon.image(state.symbol))
            .accessibilityLabel(state.headline)
            .task {
                delegate.openMainWindow = {
                    openWindow(id: KeryxWindow.main)
                    NSApp.activate()
                }
                delegate.openSetupWindow = {
                    openWindow(id: KeryxWindow.setup)
                    NSApp.activate()
                }
            }
    }
}

/// View › Refresh (⌘R): reads every document again (the phone's Twilio reads only while
/// the Phone section shows).
private struct RefreshCommand: View {
    let model: AppModel

    var body: some View {
        Button("Refresh") {
            Task { await model.refreshShown() }
        }
        .keyboardShortcut("r", modifiers: .command)
        .disabled(model.client == nil)
    }
}
