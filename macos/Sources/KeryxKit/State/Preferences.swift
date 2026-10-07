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
    }
}
