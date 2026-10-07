import AppKit
import KeryxKit
import KeryxUI

/// Owns the app's one `AppModel` and the moments AppKit tells only a delegate about: launch
/// (the menu needs Keryx found whether or not the window ever opens), a menu opening, and
/// quitting while a model downloads or the voice is being set up.
/// The app's windows, for its activation policy.
enum AppWindow: String { case main, settings, setup }

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    /// After a menu opens, the sessions and models are read again — unless they were read
    /// this recently.
    static let menuRefreshInterval: Duration = .seconds(5)

    let preferences: Preferences
    let model: AppModel

    private var windowHasAppeared = false
    private var starting: Task<Void, Never>?
    /// The app's windows open now (the main one, Settings): while any is, the app is a
    /// regular one, in the Dock and the app switcher; with none it is only a menu bar item.
    private var openWindows: Set<AppWindow> = []
    /// Opens the main window and brings the app forward. Handed over by the menu bar label,
    /// the one view alive for the app's whole life, as it appears.
    var openMainWindow: (() -> Void)?
    private var lastMenuRefresh: ContinuousClock.Instant?

    override init() {
        let preferences = Preferences()
        self.preferences = preferences
        model = AppModel(
            locator: KeryxLocator(runner: LoggingRunner()),
            makeClient: { location in
                CLIClient(location: location,
                          runner: LoggingRunner(prefix: location.prefixArguments))
            },
            preferences: preferences)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let atLogin = Self.isLoginLaunch(NSAppleEventManager.shared().currentAppleEvent)
        Log.app.info("Launched\(atLogin ? " at login" : "", privacy: .public)")
        TerminalLauncher.removeStaleScripts()
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

    /// Whether the window appearing now is one SwiftUI opened for a login launch, which
    /// should close again. Only the first appearance, and only while the launch's
    /// open-application event is still the current one (SwiftUI shows its launch window
    /// before `applicationDidFinishLaunching`): a window opened later, from the menu or by
    /// launching Keryx again, is one the owner asked for.
    func windowAppearsAtLoginLaunch() -> Bool {
        defer { windowHasAppeared = true }
        guard !windowHasAppeared, Self.isLoginLaunch(NSAppleEventManager.shared().currentAppleEvent)
        else { return false }
        Log.app.info("Closing the window opened for a login launch")
        return true
    }

    private static func isLoginLaunch(_ event: NSAppleEventDescriptor?) -> Bool {
        guard let event, event.eventID == kAEOpenApplication else { return false }
        return event.paramDescriptor(forKeyword: keyAEPropData)?.enumCodeValue == keyAELaunchedAsLogInItem
    }

    /// Launching Keryx again (from the Finder, Spotlight or `open`) while it runs with no
    /// window open opens the main window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        Log.app.info("Reopened\(hasVisibleWindows ? " with a window open" : "", privacy: .public)")
        guard !hasVisibleWindows, let openMainWindow else { return true }
        openMainWindow()
        return false
    }

    func windowDidAppear(_ window: AppWindow) {
        openWindows.insert(window)
        Log.app.info("\(window.rawValue, privacy: .public) window opened")
        NSApp.setActivationPolicy(.regular)
        NSApp.activate()
    }

    func windowDidDisappear(_ window: AppWindow) {
        openWindows.remove(window)
        Log.app.info("\(window.rawValue, privacy: .public) window closed")
        if openWindows.isEmpty { NSApp.setActivationPolicy(.accessory) }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let downloads = model.downloads
        let setup = model.voiceSetup
        guard model.isBusy else { return .terminateNow }
        // Only what is still running asks; what is already stopping is just waited for.
        let signingIn = model.signInRun.isRunning ? model.signingIn : nil
        if let question = Quitting.adding(signIn: signingIn.map(SignInWording.name),
                                          detail: signingIn.map(SignInWording.quitDetail), to: Quitting.question(
            downloading: downloads.isRunning, settingUpVoice: setup.isRunning,
            assistant: Wording.assistant(model.status.value))) {
            NSApp.activate()
            let alert = NSAlert()
            alert.messageText = question.message
            alert.informativeText = question.detail
            alert.addButton(withTitle: "Quit")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return .terminateCancel }
        }
        // Stop each child rather than leave it running without us, and wait for it to go
        // (SIGKILL follows SIGTERM within seconds); a download's part stays for next time.
        if let current = downloads.current, downloads.isRunning { downloads.cancel(current.name) }
        setup.cancel()
        model.cancelRuns()
        Task {
            let deadline = ContinuousClock.now + .seconds(8)
            while model.isBusy, ContinuousClock.now < deadline {
                try? await Task.sleep(for: .milliseconds(100))
            }
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    /// Opening the menu bar menu (or any menu that is not the main menu's, nor a pop-up
    /// button's in a window) reads the sessions and the models again, so the menu lists
    /// them as they are now.
    @objc private func menuDidBeginTracking(_ notification: Notification) {
        guard let menu = notification.object as? NSMenu, menu !== NSApp.mainMenu, menu.supermenu == nil,
              !MenuTracking.isPopUpMenu(menu, in: NSApp.windows) else { return }
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
