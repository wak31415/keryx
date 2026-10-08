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

    private enum Keys {
        static let keryxFolder = "keryxFolder"
        static let launchAgent = "launchAgentPlist"
    }
}

/// Where the locator keeps the last usable copy of Keryx's launch agent plist, to read
/// while the plist itself is gone (the app removes it with the unit when it quits).
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
