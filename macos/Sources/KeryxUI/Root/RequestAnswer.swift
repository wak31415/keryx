import KeryxKit

/// What the main window does for each `AppRequest`, and on which screen: the one table every
/// screen answers from, so no request the Setup Assistant can make goes unanswered.
///
/// Each screen watches `AppModel.requested`, asks `of(_:)` what it means, does its part when
/// the answer is its own, and calls `AppModel.didHandle(_:)`. A section that is only shown
/// (`.shown`) is answered by the main window itself.
enum RequestAnswer: Equatable {
    /// Configuration: the tab to show (the PIN's own tab when `nil`, once the settings say
    /// which), and the sheet to open there.
    case configuration(tab: String?, sheet: ConfigurationSheet?)
    /// Integrations: a Google sign-in's sheet, or a card's own Connect.
    case integrations(IntegrationsRequest)
    /// Voice: This Mac's Set Up… question.
    case voiceHere
    /// Local Models: shown at what is left to do there.
    case models
    /// Configuration, at one setting.
    case setting(String)
    /// Plugins, at one plugin.
    case plugin(String)
    /// Phone: a sheet of its own.
    case phone(PhoneRequest)
    /// The section shown, and nothing more.
    case shown(SidebarItem)

    /// The tab the coding agents are set up on, where their sign-ins are.
    static let agentsTab = "agents"

    static func of(_ request: AppRequest) -> RequestAnswer {
        switch request {
        case .show(.models): .models
        case let .show(item): .shown(item)
        case let .configure(tab): .configuration(tab: tab, sheet: nil)
        case .setPin: .configuration(tab: nil, sheet: .pin)
        case let .signIn(provider):
            switch provider {
            case .claude, .codex: .configuration(tab: agentsTab, sheet: .signIn(provider))
            case .gmail, .googleWorkspace: .integrations(.signIn(provider))
            }
        case let .connect(id): .integrations(.connect(id))
        case .setUpVoiceHere: .voiceHere
        case let .setting(key): .setting(key)
        case let .plugin(name): .plugin(name)
        case .phoneAccount: .phone(.account)
        case .pointHere: .phone(.pointHere)
        }
    }

    /// The section whose screen does it.
    var section: SidebarItem {
        switch self {
        case .configuration, .setting: .configuration
        case .integrations: .integrations
        case .plugin: .plugins
        case .phone: .phone
        case .voiceHere: .voice
        case .models: .models
        case let .shown(item): item
        }
    }
}

/// A sheet Configuration opens for a request.
enum ConfigurationSheet: Equatable {
    /// The PIN sheet, through `PinFlow`.
    case pin
    /// A coding agent's sign-in (Claude's or Codex's), from the Agents tab.
    case signIn(SignInProvider)
}

/// What Integrations does for a request.
enum IntegrationsRequest: Equatable {
    /// Gmail's or Google for agents' sign-in sheet.
    case signIn(SignInProvider)
    /// The Connect of the card with this id (`google`, `github`, …).
    case connect(String)
}

/// What Phone does for a request.
enum PhoneRequest: Equatable {
    /// The Twilio account sheet.
    case account
    /// The Point Here question, once Twilio has said where the number points.
    case pointHere
}
