import ServiceManagement

/// Whether Keryx opens at login: the app itself registered as a login item.
///
/// The registration names this copy of the app by its signature, so it works from
/// /Applications, and an ad-hoc signed rebuild needs it turned off and on again.
@MainActor
enum LoginItem {
    enum State: Equatable {
        case on, off
        /// Registered, but the owner has yet to allow it in System Settings.
        case needsApproval
    }

    static var state: State {
        switch SMAppService.mainApp.status {
        case .enabled: .on
        case .requiresApproval: .needsApproval
        default: .off
        }
    }

    static func set(_ on: Bool) throws {
        if on {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
    }

    /// System Settings › General › Login Items.
    static func openSystemSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }
}
