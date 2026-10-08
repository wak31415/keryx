import Foundation

/// What the owner chose in Settings, kept in `UserDefaults`.
///
/// `@unchecked Sendable`: its only state is the `UserDefaults`, which is documented as safe
/// to use from any thread.
public final class Preferences: @unchecked Sendable {
    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    /// A Keryx checkout chosen by hand, which the locator tries before anything else.
    public var keryxFolder: String? {
        get { defaults.string(forKey: Keys.keryxFolder) }
        set {
            if let newValue {
                defaults.set(newValue, forKey: Keys.keryxFolder)
            } else {
                defaults.removeObject(forKey: Keys.keryxFolder)
            }
        }
    }

    /// Whether a plain Quit asks first ("Quit Keryx?"), until the owner ticks "Don't ask
    /// again". A quit with a call in progress, or with something under way, always asks.
    public var asksBeforeQuitting: Bool {
        get { !defaults.bool(forKey: Keys.quitWithoutAsking) }
        set { defaults.set(!newValue, forKey: Keys.quitWithoutAsking) }
    }

    /// Quit's detached `service stop --all`, recorded as it is spawned, until the next launch
    /// has waited for it (or found it finished).
    public var detachedStop: DetachedStop? {
        get {
            guard let data = defaults.data(forKey: Keys.detachedStop) else { return nil }
            return try? JSONDecoder().decode(DetachedStop.self, from: data)
        }
        set {
            if let newValue, let data = try? JSONEncoder().encode(newValue) {
                defaults.set(data, forKey: Keys.detachedStop)
            } else {
                defaults.removeObject(forKey: Keys.detachedStop)
            }
        }
    }

    /// Records the detached run `pid` as Quit's stop: a handler for
    /// `ProcessRunner(onDetachedSpawn:)`.
    public func recordDetachedStop(pid: Int32) {
        detachedStop = DetachedStop.of(pid: pid)
    }

    /// What was typed for a voice LLM on a server elsewhere — its address, its model — and
    /// not yet checked and used: kept here, in the app's own preferences, and never in
    /// Keryx's settings (`VOICE_LLM_BASE_URL` ships every call to whoever is there, so it is
    /// written only by Check and Use, once the server has answered). An empty draft is none.
    public var voiceLLMDraftAddress: String? {
        get { defaults.string(forKey: Keys.voiceLLMDraftAddress) }
        set { setDraft(newValue, forKey: Keys.voiceLLMDraftAddress) }
    }

    /// The model typed for a voice LLM elsewhere, as `voiceLLMDraftAddress` is kept.
    public var voiceLLMDraftModel: String? {
        get { defaults.string(forKey: Keys.voiceLLMDraftModel) }
        set { setDraft(newValue, forKey: Keys.voiceLLMDraftModel) }
    }

    private func setDraft(_ value: String?, forKey key: String) {
        let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if trimmed.isEmpty { defaults.removeObject(forKey: key) } else { defaults.set(trimmed, forKey: key) }
    }

    private enum Keys {
        static let voiceLLMDraftAddress = "voiceLLMDraftAddress"
        static let voiceLLMDraftModel = "voiceLLMDraftModel"
        static let detachedStop = "detachedStop"
        static let quitWithoutAsking = "quitWithoutAsking"
        static let keryxFolder = "keryxFolder"
        static let launchAgent = "launchAgentPlist"
    }
}

/// Where the locator keeps the last usable copy of Keryx's launch agent plist, to read
/// while the plist itself is gone (removed at a terminal, or by `keryx service uninstall`).
public protocol LaunchAgentMemory: Sendable {
    func rememberLaunchAgent(_ plist: Data)
    func recallLaunchAgent() -> Data?
}

extension Preferences: LaunchAgentMemory {
    public func rememberLaunchAgent(_ plist: Data) {
        if defaults.data(forKey: Keys.launchAgent) != plist { defaults.set(plist, forKey: Keys.launchAgent) }
    }

    public func recallLaunchAgent() -> Data? {
        defaults.data(forKey: Keys.launchAgent)
    }
}
