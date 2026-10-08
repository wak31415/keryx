import KeryxUI
import Security
import ServiceManagement

/// Whether Keryx opens at login: the app itself registered as a login item.
///
/// The registration names this copy of the app by its signature, so it works from
/// /Applications. Signed with "Keryx Local Signing" (`make-signing-identity.sh`), every
/// build has the same designated requirement and the registration survives a rebuild;
/// signed ad hoc, a rebuild needs it turned off and on again.
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

    /// The Settings window's handle on it.
    static var control: LoginItemControl {
        LoginItemControl(
            state: {
                switch state {
                case .on: .on
                case .off: .off
                case .needsApproval: .needsApproval
                }
            },
            set: { try set($0) },
            openSystemSettings: { openSystemSettings() },
            survivesRebuilds: signedWithIdentity)
    }

    /// Whether this copy is signed with a certificate (the local identity), not ad hoc: an
    /// ad-hoc signature has none.
    static var signedWithIdentity: Bool {
        var code: SecCode?
        var staticCode: SecStaticCode?
        var info: CFDictionary?
        guard SecCodeCopySelf([], &code) == errSecSuccess, let code,
              SecCodeCopyStaticCode(code, [], &staticCode) == errSecSuccess, let staticCode,
              SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &info)
              == errSecSuccess,
              let signing = info as? [String: Any],
              let certificates = signing[kSecCodeInfoCertificates as String] as? [SecCertificate]
        else { return false }
        return !certificates.isEmpty
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
