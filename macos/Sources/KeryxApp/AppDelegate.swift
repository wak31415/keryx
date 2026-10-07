import AppKit
import KeryxKit

/// Owns the app's one `AppModel` and the moments AppKit tells only a delegate about: launch
/// (the menu needs Keryx found whether or not the window ever opens), a menu opening, and
/// quitting while a model downloads.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    /// After a menu opens, the sessions and models are read again — unless they were read
    /// this recently.
    static let menuRefreshInterval: Duration = .seconds(5)

    let preferences: Preferences
    let model: AppModel

    /// Launched at login (by `LoginItem`): the window SwiftUI opens at launch closes again,
    /// and Keryx starts in the menu bar.
    private var launchedAtLogin = false
    private var windowHasAppeared = false
    private var starting: Task<Void, Never>?
    private var lastMenuRefresh: ContinuousClock.Instant?

    override init() {
        let preferences = Preferences()
        self.preferences = preferences
        model = AppModel(
            locator: KeryxLocator(runner: LoggingRunner()),
            makeClient: { location in
                CLIClient(location: location,
                          runner: LoggingRunner(prefixCount: location.prefixArguments.count))
            },
            preferences: preferences)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        launchedAtLogin = Self.isLoginLaunch(NSAppleEventManager.shared().currentAppleEvent)
        Log.app.info("Launched\(self.launchedAtLogin ? " at login" : "", privacy: .public)")
        NotificationCenter.default.addObserver(
            self, selector: #selector(menuDidBeginTracking(_:)),
            name: NSMenu.didBeginTrackingNotification, object: nil)
        Task { await start() }
    }

    /// Finds Keryx and starts the status poll, then reads what the menu lists that the poll
    /// does not: the sessions and the local models. Launch and the window both ask; it runs
    /// once, and a second caller waits for the first.
    func start() async {
        if starting == nil {
            starting = Task {
                await model.start()
                logLocation()
                await refreshMenuDocuments()
            }
        }
        await starting?.value
    }

    /// Whether the window appearing now is the one SwiftUI opened at a login launch, which
    /// should close again: only ever its first appearance. That comes while the launch's
    /// open-application event is still current, before `applicationDidFinishLaunching`, so
    /// the event is read here as well as there.
    func windowAppearsAtLoginLaunch() -> Bool {
        defer { windowHasAppeared = true }
        guard !windowHasAppeared else { return false }
        return launchedAtLogin || Self.isLoginLaunch(NSAppleEventManager.shared().currentAppleEvent)
    }

    private static func isLoginLaunch(_ event: NSAppleEventDescriptor?) -> Bool {
        guard let event, event.eventID == kAEOpenApplication else { return false }
        return event.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard model.downloads.isRunning else { return .terminateNow }
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = "A model is downloading. Quit anyway?"
        alert.informativeText = "It will pick up where it left off next time."
        alert.addButton(withTitle: "Quit")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return .terminateCancel }
        // Stop the download's child rather than leave it running without us, and give it a
        // moment to go; the part already downloaded stays for next time.
        if let current = model.downloads.current { model.downloads.cancel(current.name) }
        Task {
            try? await Task.sleep(for: .milliseconds(500))
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    /// Opening the menu bar menu (or any menu that is not the main menu's) reads the
    /// sessions and the models again, so the menu lists them as they are now.
    @objc private func menuDidBeginTracking(_ notification: Notification) {
        guard let menu = notification.object as? NSMenu, menu !== NSApp.mainMenu, menu.supermenu == nil else { return }
        let now = ContinuousClock.now
        if let last = lastMenuRefresh, now - last < Self.menuRefreshInterval { return }
        lastMenuRefresh = now
        Task { await refreshMenuDocuments() }
    }

    private func refreshMenuDocuments() async {
        guard model.client != nil else { return }
        async let tasks: Void = model.refresh(.tasks)
        async let models: Void = model.refresh(.models)
        _ = await (tasks, models)
    }

    private func logLocation() {
        if let location = model.location.value {
            Log.app.info("Found Keryx (\(location.source.rawValue, privacy: .public))")
        } else if model.location.error != nil {
            Log.app.notice("Keryx was not found")
        }
    }
}
