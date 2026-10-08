import Foundation
import KeryxKit

/// What the CLI says would mend a check, as the app does it: `doctor --json`'s and `setup
/// plan --json`'s `fix`, one id from a closed set (`keryx/fixes.py`), turned into a button —
/// its title, and what it does. The one mapping every screen that shows a check or a step
/// uses (the Setup Assistant, the Overview's Setup section), so the same problem always
/// offers the same button.
///
/// An id this build does not know is `nil`: no button, never a guess.
enum FixAction: Equatable, Sendable {
    /// `signin:<name>`: one of `keryx auth login`'s sign-ins.
    case signIn(SignInProvider)
    /// `secret:<KEY>`: a setting kept in `secrets.toml`, typed into the secret sheet.
    case secret(String)
    /// `setting:<KEY>`: any other setting, where Configuration (or its own screen) edits it.
    case setting(String)
    /// `plugin:<name>`: a plugin, on the Plugins screen.
    case plugin(String)
    /// `integration:<id>`: an account's card on Integrations, connected there.
    case integration(String)
    /// `pin`: the PIN sheet.
    case pin
    /// `service:install`: Keryx started answering calls. The app starts it by itself once
    /// setup allows; the button is for a start that failed, and asks nothing first.
    case startKeryx
    /// `models:start`: the local model server, started.
    case startModelServer
    /// `models:choose`: a local model to choose (and download), on Local Models.
    case chooseModel
    /// `voice:here`: the voice server set up on this Mac.
    case voiceHere
    /// `voice:download`: the voice's speech models downloaded.
    case voiceDownload
    /// `phone:account`: the Twilio account sheet.
    case phoneAccount
    /// `phone:webhooks`: the Twilio number pointed here, after its question.
    case phoneWebhooks
    /// `phone:tunnel`: a tunnel installed, or its hostname set, on Phone.
    case phoneTunnel
    /// `migrate`: Jarvis's files moved.
    case migrate

    /// The ids that take no argument, as the CLI spells them (`fixes.ACTIONS`).
    static let screens: [String: FixAction] = [
        "pin": .pin, "service:install": .startKeryx, "models:start": .startModelServer,
        "models:choose": .chooseModel, "voice:here": .voiceHere, "voice:download": .voiceDownload,
        "phone:account": .phoneAccount, "phone:webhooks": .phoneWebhooks, "phone:tunnel": .phoneTunnel,
        "migrate": .migrate,
    ]

    /// `id` parsed; `nil` for null, or an id this build does not know.
    init?(_ id: String?) {
        guard let id else { return nil }
        if let screen = Self.screens[id] {
            self = screen
            return
        }
        let parts = id.split(separator: ":", maxSplits: 1).map(String.init)
        guard parts.count == 2, !parts[1].isEmpty else { return nil }
        let argument = parts[1]
        switch parts[0] {
        case "signin":
            guard let provider = SignInProvider(rawValue: argument) else { return nil }
            self = .signIn(provider)
        case "secret": self = .secret(argument)
        case "setting": self = .setting(argument)
        case "plugin": self = .plugin(argument)
        case "integration": self = .integration(argument)
        default: return nil
        }
    }

    /// The button: its title and what it does, worded with what the app knows (a setting's
    /// title, whether a PIN or a key is already set, whether a tunnel program is installed).
    /// `named`: the title of the check it mends ("OpenAI API key"), for a key the settings
    /// have not been read for yet. `missing`: that check says it is not set, whatever the
    /// settings read before it said — a secret's button then adds rather than changes.
    func action(_ context: SetupContext, named: String? = nil, missing: Bool = false) -> SetupAction {
        switch self {
        case let .signIn(provider):
            return SetupAction(title: "Sign In to \(SignInWording.name(provider))…", effect: .request(.signIn(provider)))
        case let .secret(key):
            let row = context.row(key)
            let title = row?.displayTitle ?? named ?? FixAction.humanised(key)
            let replacing = row?.set == true && !missing
            return SetupAction(title: "\(replacing ? "Change" : "Add") \(Wording.titleCase(title))…",
                               effect: .secret(key: key, title: title, replacing: replacing))
        case let .setting(key):
            switch key {
            case "VOICE_BASE_URL":
                return open("Open Voice", .show(.voice))
            case "LOCAL_AGENT_BASE_URL", "LOCAL_AGENT_MODEL":
                return open("Open Local Models", .show(.models))
            default:
                let title = context.row(key)?.displayTitle ?? named ?? FixAction.humanised(key)
                return open("Open \(Wording.titleCase(title))", .setting(key: key))
            }
        case let .plugin(name):
            return open("Open \(context.pluginTitle(name).map(Wording.titleCase) ?? "Plugins")", .plugin(name: name))
        case let .integration(id):
            let title = context.integrationTitle(id) ?? FixAction.integrationNames[id] ?? FixAction.humanised(id)
            return open("Connect \(Wording.titleCase(title))…", .connect(integration: id))
        case .pin:
            return open(context.pin?.set == true ? "Change PIN…" : "Set PIN…", .setPin)
        case .startKeryx:
            return SetupAction(title: "Start Keryx", effect: .startKeryx)
        case .startModelServer:
            // Running another model: started again, on the one chosen.
            let restart = context.modelServerServesAnother != nil
            return SetupAction(title: restart ? "Restart Model Server" : "Start Model Server", effect: .startModelServer)
        case .chooseModel:
            return open("Choose a Model", .show(.models))
        case .voiceHere:
            return open("Set Up Voice on This Mac…", .setUpVoiceHere)
        case .voiceDownload:
            return SetupAction(title: "Download Speech Models", effect: .downloadVoiceModels)
        case .phoneAccount:
            return open("Connect Twilio…", .phoneAccount)
        case .phoneWebhooks:
            return open("Point Number Here…", .pointHere)
        case .phoneTunnel:
            if let page = context.tunnelDownload {
                // A page, not an installer: the app installs no tunnel itself.
                return SetupAction(title: "How to Install…", effect: .openURL(page))
            }
            return open("Open Phone", .show(.phone))
        case .migrate:
            return open("Migrate…", .show(.overview))
        }
    }

    private func open(_ title: String, _ request: AppRequest) -> SetupAction {
        SetupAction(title: title, effect: .request(request))
    }

    /// The accounts' names before `integrations --json` has been read.
    static let integrationNames: [String: String] = [
        "google": "Google", "gmail": "Gmail", "slack": "Slack", "github": "GitHub",
        "openai-admin": "OpenAI admin key", "anthropic-admin": "Anthropic admin key",
    ]

    /// A key or id as words when nothing names it better: `OWNER_NAME` → "Owner name",
    /// `OPENAI_API_KEY` → "OpenAI API key".
    static func humanised(_ key: String) -> String {
        let words = key.split(whereSeparator: { $0 == "_" || $0 == "-" }).map { word -> String in
            let lower = word.lowercased()
            if let known = spellings[lower] { return known }
            return lower
        }
        guard let first = words.first else { return key }
        return ([first.prefix(1).uppercased() + first.dropFirst()] + words.dropFirst()).joined(separator: " ")
    }

    /// Words a key holds that are not spelt in lower case.
    static let spellings: [String: String] = [
        "openai": "OpenAI", "api": "API", "url": "URL", "pin": "PIN", "id": "ID", "sid": "SID",
        "github": "GitHub", "slack": "Slack", "anthropic": "Anthropic", "codex": "Codex", "twilio": "Twilio",
        "keryx": "Keryx", "google": "Google", "gmail": "Gmail",
    ]

    /// The pages a tunnel may be downloaded from: the CLI's `install_url` is opened only when
    /// it is one of these, never whatever a CLI printed.
    static let tunnelHosts: Set<String> = ["developers.cloudflare.com", "ngrok.com", "dashboard.ngrok.com"]

    /// Checked as a sign-in's page is (`TrustedLink`): https, one of these hosts, and no
    /// user, password or port.
    static func tunnelPage(_ text: String?) -> URL? {
        text.flatMap { TrustedLink.url($0, on: tunnelHosts) }
    }
}
