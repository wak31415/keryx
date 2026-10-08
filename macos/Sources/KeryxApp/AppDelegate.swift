import AppKit
import KeryxKit
import KeryxUI

/// Owns the app's one `AppModel` and the moments AppKit tells only a delegate about: launch
/// (the menu needs Keryx found whether or not the window ever opens, and Keryx started once
/// setup allows), a menu opening, and quitting, which stops Keryx answering calls — and asks
/// first while a call is in progress or something is under way.
/// The app's windows, for the log.
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
    /// The app's windows open now (the main one, Settings, the Setup Assistant). The app is
    /// in the Dock whether or not any is (`AppLifecycle`).
    private var openWindows: Set<AppWindow> = []
    /// Opens the main window and brings the app forward. Handed over by the menu bar label,
    /// the one view alive for the app's whole life, as it appears.
    var openMainWindow: (() -> Void)?
    /// Opens the setup wizard in a mode and brings the app forward; handed over as
    /// `openMainWindow` is.
    var openSetupWindow: ((SetupEntry) -> Void)?
    private var launchedAtLogin = false
    /// The wizard has opened by itself this launch; it does so once at most.
    private var setupOpenedAtLaunch = false
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
        // Kept answering calls while the app runs — but never by a sandbox run, whose `keryx`
        // would install or remove the real launch agents.
        model.keepsAnswering = AppModel.keepsAnswering(environment: ProcessInfo.processInfo.environment)
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let atLogin = Self.isLoginLaunch(NSAppleEventManager.shared().currentAppleEvent)
        launchedAtLogin = atLogin
        Log.app.info("Launched\(atLogin ? " at login" : "", privacy: .public)")
        NSApp.setActivationPolicy(AppLifecycle.activationPolicy)
        if !model.keepsAnswering {
            Log.app.notice("KERYX_APP_ENVIRONMENT=inherit: Keryx is neither started nor stopped by this run")
        }
        TerminalLauncher.removeStaleScripts()
        NotificationCenter.default.addObserver(
            self, selector: #selector(menuDidBeginTracking(_:)),
            name: NSMenu.didBeginTrackingNotification, object: nil)
        Task {
            await start()
            await openSetupIfNeeded()
        }
    }

    /// Once, after launch, until setup has been seen through: opens the setup wizard while
    /// the plan says Keryx is not ready for a first call — not at a login launch, which opens
    /// no window, and not once the owner has ticked "Don't show this again".
    private func openSetupIfNeeded() async {
        guard !launchedAtLogin, !setupOpenedAtLaunch, !SetupLaunch.isHidden(), !SetupLaunch.completedOnce(),
              model.client != nil else { return }
        await model.refresh(.setupPlan)
        guard SetupLaunch.atLaunch(plan: model.setupPlan.value, hidden: SetupLaunch.isHidden(),
                                   alreadyOpened: setupOpenedAtLaunch) else { return }
        setupOpenedAtLaunch = true
        Log.app.info("Opening the setup wizard: setup is not finished")
        openSetupWindow?(.launch)
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
        NSApp.activate()
    }

    /// A window closed — the red button, ⌘W: the app runs on, in the Dock and the menu bar,
    /// and Keryx goes on answering calls.
    func windowDidDisappear(_ window: AppWindow) {
        openWindows.remove(window)
        Log.app.info("\(window.rawValue, privacy: .public) window closed")
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        AppLifecycle.quitsWhenLastWindowCloses
    }

    /// How long quitting lets a short write (a setting, the PIN, the webhooks) finish before
    /// it asks about it.
    static let writeGrace: Duration = .seconds(5)
    /// How long quitting, at Cmd+Q only, waits for a voice LLM stopped part way to put back
    /// what it was replacing before anything is killed. Session end skips this: launchd
    /// tears the GUI domain down by itself, and nothing here would be given the time anyway.
    static let voiceLLMPutBackBound: Duration = .seconds(30)

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // Moving Jarvis's files is finished before anything else: killed part way, it would
        // leave the service stopped and the files half moved.
        if model.migrationRun.isRunning {
            NSApp.activate()
            let alert = NSAlert()
            alert.messageText = Quitting.migratingTitle
            alert.informativeText = Quitting.migratingDetail
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return .terminateCancel
        }
        let children = ChildRegistry.shared
        // Whether `service stop --all` runs at all (always, while Keryx is kept answering —
        // the CLI handles nothing being installed itself) is a different question from
        // whether quitting says it stops *answering calls* (only the agent's own unit).
        let runsStop = model.quitRunsServiceStop
        let stopsAnswering = model.quitStopsAnswering
        guard runsStop || model.isBusy || children.count > 0 || !model.writes.isEmpty else { return .terminateNow }
        let sessionEnding = AppLifecycle.isSessionEnding(NSAppleEventManager.shared().currentAppleEvent)
        // At a log out, restart or shut down launchd tears the GUI domain down by itself, so
        // the units are let go rather than the reply held for a stop.
        Task {
            await quit(children, stopping: runsStop && !sessionEnding,
                      stopsAnswering: stopsAnswering && !sessionEnding, asking: !sessionEnding)
        }
        return .terminateLater
    }

    /// Lets a short write finish first; asks while anything that quitting would cut short is
    /// still going — the long runs, a write that outlived its moment, a call in progress — or,
    /// when it stops Keryx answering, until the owner says not to ask again; and on a yes
    /// stops every child, waits for each group to go, and stops Keryx (`stopping`). At a log
    /// out, restart or shut down it asks nothing (`asking` false) and stops nothing of Keryx's
    /// (`stopping` false): launchd ends the session's agents itself. `stopsAnswering` is only
    /// the question's wording (`quitStopsAnswering`): `stopping` runs the stop regardless.
    private func quit(_ children: ChildRegistry, stopping: Bool, stopsAnswering: Bool, asking: Bool) async {
        let downloads = model.downloads
        let setup = model.voiceSetup
        // A write is a moment's work: killed part way it can leave a change half made.
        if !model.writes.isEmpty { _ = await model.writesFinish(within: Self.writeGrace) }
        // Only what is still running asks; what is already stopping is just waited for.
        // A model server started by This Mac's Set Up… is part of setting the voice up.
        let server = model.modelServerRun.isRunning
        let chained = server && model.thisMacSetup?.startsServer == true
        let signingIn = model.signInRun.isRunning ? model.signingIn : nil
        if asking, let question = Quitting.question(downloading: downloads.isRunning,
                                                    settingUpVoice: setup.isRunning || chained,
                                                    startingModelServer: server && !chained,
                                                    downloadingVoiceModels: model.voiceDownloadRun.isRunning,
                                                    changingVoiceLLM: model.voiceLLMRun.isRunning,
                                                    startingVoiceLLMServer: model.voiceLLMServerRun.isRunning,
                                                    signingIn: signingIn.map(SignInWording.name),
                                                    signInDetail: signingIn.map(SignInWording.quitDetail),
                                                    assistant: Wording.assistant(model.status.value),
                                                    changingService: model.serviceChange,
                                                    writing: model.writing, stopsAnswering: stopsAnswering,
                                                    liveCalls: model.status.value?.health.liveSessions ?? 0,
                                                    confirm: preferences.asksBeforeQuitting) {
            NSApp.activate()
            let alert = NSAlert()
            alert.messageText = question.message
            alert.informativeText = question.detail
            alert.addButton(withTitle: "Quit")
            alert.addButton(withTitle: "Cancel")
            if question.suppressible {
                alert.showsSuppressionButton = true
                alert.suppressionButton?.title = "Don't ask again"
            }
            guard alert.runModal() == .alertFirstButtonReturn else {
                NSApp.reply(toApplicationShouldTerminate: false)
                return
            }
            if question.suppressible, alert.suppressionButton?.state == .on {
                preferences.asksBeforeQuitting = false
            }
        }
        // Nothing starts Keryx again from here on, and the poll stops.
        model.prepareToQuit()
        // Stop every child rather than leave it running without us — the runs above, and
        // whatever else is still going (a write, an installer, a read) — and wait until each
        // group is gone (SIGKILL follows SIGTERM within seconds); a download's part stays for
        // next time. The deadline outlasts the bounded stop, and is the last resort.
        if let current = downloads.current, downloads.isRunning { downloads.cancel(current.name) }
        setup.cancel()
        model.cancelRuns()
        // A voice LLM stopped part way puts back what it was replacing, with Cancel held
        // off — but only at Cmd+Q (`asking`): at session end there is no time to give it.
        if asking { _ = await model.voiceLLMRunsEnd(within: Self.voiceLLMPutBackBound) }
        let survivors = await children.stopAll(within: ChildRegistry.stopBound + .seconds(3))
        if let survivors, !survivors.isEmpty {
            Log.app.error("Quitting with \(survivors.count) process group(s) still running after SIGKILL: \(survivors.map(String.init).joined(separator: ", "), privacy: .public)")
        } else if survivors == nil {
            Log.app.error("Quitting before every process group had stopped: \(children.count) still listed")
        }
        if stopping { await stopAnswering() }
        NSApp.reply(toApplicationShouldTerminate: true)
    }

    /// Stops Keryx answering calls (`AppModel.stopAnswering`), which is itself bounded
    /// (`AppModel.detachedStopBound`) and runs the stop detached from the app, so nothing
    /// here waits any longer or cuts anything short.
    private func stopAnswering() async {
        let error = await model.stopAnswering()
        if let error {
            Log.app.error("Keryx could not be stopped: \(error.title, privacy: .public)")
        } else {
            Log.app.info("Stopped Keryx answering calls")
        }
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
