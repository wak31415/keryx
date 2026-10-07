import Foundation

/// Something one window asks the main window to do: show a section and, for most, open the
/// sheet or start the setup that settles it there. The Setup Assistant asks; the screen at
/// `destination` answers.
///
/// `AppModel.request(_:)` selects the destination and holds the request in
/// `AppModel.requested`. The screen that can answer it watches `requested`, does it, and
/// calls `AppModel.didHandle(_:)`. A request nobody answers has still shown its section,
/// which is the whole of `.show`.
public enum AppRequest: Equatable, Sendable {
    /// Show a section; nothing more.
    case show(SidebarItem)
    /// Configuration, on the tab with this id (`general`, `agents`, … as `config list`
    /// names them).
    case configure(tab: String)
    /// The sign-in sheet for `provider`.
    case signIn(SignInProvider)
    /// Connect the integration with this id in `keryx integrations` (`google`, `github`, …):
    /// its card's Connect.
    case connect(integration: String)
    /// The PIN sheet: set the phone PIN.
    case setPin
    /// The Voice section's setup on this Mac (`keryx voice use here`).
    case setUpVoiceHere

    /// The section the main window shows for it.
    public var destination: SidebarItem {
        switch self {
        case let .show(item): item
        case .configure, .setPin: .configuration
        case let .signIn(provider):
            switch provider {
            case .claude, .codex: .configuration
            case .gmail, .googleWorkspace: .integrations
            }
        case .connect: .integrations
        case .setUpVoiceHere: .voice
        }
    }
}
