import AppKit
import KeryxKit
import KeryxUI
import SwiftUI

/// Keryx for Mac: a menu bar item that is always there, a window opened from it, and the
/// app's own Settings. An `LSUIElement` app — it is in the Dock only while its window is open.
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

        MenuBarExtra {
            MenuBarMenu(delegate: delegate)
                .environment(delegate.model)
        } label: {
            MenuBarLabel(model: delegate.model)
        }
        .menuBarExtraStyle(.menu)

        Settings {
            AppSettingsView()
                .environment(delegate.model)
                .environment(\.preferences, delegate.preferences)
        }
    }
}

enum KeryxWindow {
    static let main = "main"
}

/// The window's content. While it is open the app is a regular one, in the Dock and the
/// app switcher; when it closes the app goes back to being only a menu bar item. SwiftUI
/// opens it at launch; at a login launch it closes again at once.
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
                NSApp.setActivationPolicy(.regular)
                NSApp.activate()
            }
            .onDisappear {
                NSApp.setActivationPolicy(.accessory)
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
            quit: { NSApp.terminate(nil) }))
    }

    private func open() {
        openWindow(id: KeryxWindow.main)
        NSApp.activate()
    }
}

/// The menu bar image: the mark's bars, with a dot or a slash for what is worth knowing.
private struct MenuBarLabel: View {
    let model: AppModel

    var body: some View {
        Image(nsImage: MenuBarIcon.image(model.menuState.symbol))
            .accessibilityLabel(model.menuState.headline)
    }
}

/// View › Refresh (⌘R): reads every document again.
private struct RefreshCommand: View {
    let model: AppModel

    var body: some View {
        Button("Refresh") {
            Task { await model.refresh(.all) }
        }
        .keyboardShortcut("r", modifiers: .command)
        .disabled(model.client == nil)
    }
}
