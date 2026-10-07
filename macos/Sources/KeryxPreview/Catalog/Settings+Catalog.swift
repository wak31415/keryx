import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The app's own Settings window (⌘,): Keryx found from its service, with the login
    /// item on; and the login item waiting to be allowed in System Settings.
    @MainActor static var settingsEntries: [Entry] {
        let size = CGSize(width: 460, height: 360)
        return [
            Entry("settings", size: size) {
                AppSettingsView(loginItem: loginItem(.on)).environment(AppModel.preview())
            },
            Entry("settings-login-needs-approval", size: size) {
                AppSettingsView(loginItem: loginItem(.needsApproval)).environment(AppModel.preview())
            },
        ]
    }

    /// A login item that stays as it is: nothing here registers the snapshot tool.
    private static func loginItem(_ state: LoginItemControl.State) -> LoginItemControl {
        LoginItemControl(state: { state }, set: { _ in }, openSystemSettings: {})
    }
}
