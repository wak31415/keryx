import AppKit

/// How the app lives beside its windows. It is a regular app — `LSUIElement` is not set in
/// its Info.plist — so it is in the Dock, with its running dot, for as long as it runs,
/// window or not, as well as in the menu bar. Closing a window (the red button, ⌘W) closes
/// only the window, and Keryx goes on answering calls; Quit (⌘Q) quits the app and stops
/// Keryx answering. A login launch opens no window, but the Dock icon and its dot are there.
public enum AppLifecycle {
    /// Always `.regular`, whichever windows are open.
    public static let activationPolicy: NSApplication.ActivationPolicy = .regular

    /// Closing the last window never quits the app.
    public static let quitsWhenLastWindowCloses = false

    /// Whether the quit Apple event `event` is the session ending — log out, restart or shut
    /// down — when quitting asks nothing and does not stop Keryx: nobody is there to answer,
    /// launchd ends the session's agents itself, and neither a question nor an uninstall may
    /// hold the logout up. The agents stay installed, so they load again at the next login.
    public static func isSessionEnding(_ event: NSAppleEventDescriptor?) -> Bool {
        guard let event, event.eventID == AEEventID(kAEQuitApplication),
              let reason = event.attributeDescriptor(forKeyword: AEKeyword(kAEQuitReason))?.enumCodeValue
        else { return false }
        return sessionEndingReasons.contains(reason)
    }

    /// The quit reasons that are the session ending.
    static let sessionEndingReasons: Set<OSType> = [
        OSType(kAELogOut), OSType(kAEReallyLogOut), OSType(kAEShowRestartDialog), OSType(kAERestart),
        OSType(kAEShowShutdownDialog), OSType(kAEShutDown),
    ]
}
