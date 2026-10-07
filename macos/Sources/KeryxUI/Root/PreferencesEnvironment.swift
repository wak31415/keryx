import KeryxKit
import SwiftUI

extension EnvironmentValues {
    /// Where the owner's choices are kept; the app sets the one its `AppModel` reads.
    public var preferences: Preferences {
        get { self[PreferencesKey.self] }
        set { self[PreferencesKey.self] = newValue }
    }
}

private struct PreferencesKey: EnvironmentKey {
    static let defaultValue = Preferences()
}
