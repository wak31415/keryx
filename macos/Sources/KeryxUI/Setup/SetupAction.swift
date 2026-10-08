import Foundation
import KeryxKit

/// What a fix's button does, wherever one is shown (the Overview's Setup Checklist, the setup
/// wizard's Checking frame): `FixAction`'s button, run by `FixPerformer`.
struct SetupAction: Equatable, Identifiable, Sendable {
    enum Effect: Equatable, Sendable {
        /// Asks the main window (`AppModel.request`) and brings it forward.
        case request(AppRequest)
        /// The secret sheet for `key`, where the button is.
        case secret(key: String, title: String, replacing: Bool)
        /// Keryx started answering calls (`AppModel.startAnswering`), with no question: the app
        /// keeps it running while it is open.
        case startKeryx
        /// `keryx models start`, followed here.
        case startModelServer
        /// `keryx models start --voice-llm`, followed on Local Models.
        case startVoiceLLMServer
        /// `keryx voice llm here NAME`: a voice LLM named but not on disk, downloaded and
        /// started; followed on Local Models.
        case setUpVoiceLLM(String)
        /// `keryx voice download`, followed on Local Models.
        case downloadVoiceModels
        /// A page in the browser: one the app trusts (`FixAction.tunnelPage`).
        case openURL(URL)
    }

    var title: String
    var effect: Effect

    var id: String { title }
}

/// What is known beside a check, to word the button that mends it: the settings, the voice,
/// the local models, the PIN and the units.
struct SetupContext: Sendable {
    var config: [ConfigRow]?
    var voice: VoiceStatus?
    var models: ModelsReport?
    var pin: PinStatus?
    var service: ServiceReport?
    var phoneTunnel: PhoneTunnel?
    var integrations: IntegrationsReport?
    var plugins: PluginsReport?

    @MainActor init(_ model: AppModel) {
        self.init(config: model.config.value, voice: model.voice.value, models: model.models.value,
                  pin: model.pin.value, service: model.service.value, phoneTunnel: model.phoneTunnel.value,
                  integrations: model.integrations.value, plugins: model.plugins.value)
    }

    init(config: [ConfigRow]? = nil, voice: VoiceStatus? = nil, models: ModelsReport? = nil,
         pin: PinStatus? = nil, service: ServiceReport? = nil, phoneTunnel: PhoneTunnel? = nil,
         integrations: IntegrationsReport? = nil, plugins: PluginsReport? = nil) {
        self.config = config
        self.voice = voice
        self.models = models
        self.pin = pin
        self.service = service
        self.phoneTunnel = phoneTunnel
        self.integrations = integrations
        self.plugins = plugins
    }

    /// cloudflared's download page, while no tunnel program is installed.
    var tunnelDownload: URL? { FixAction.tunnelPage(phoneTunnel?.installUrl) }

    /// The model this Mac's model server is serving when it runs one other than the model
    /// chosen (`AGENT_SERVER_MODEL`), by its title; `nil` when it isn't running, says nothing,
    /// or serves the one chosen.
    var modelServerServesAnother: String? {
        guard let llm = models?.servers.llm, llm.reachable, let served = llm.servedModel, !served.isEmpty,
              let chosen = llm.model, served != chosen else { return nil }
        return models?.models.first { $0.name == served }?.title ?? served
    }

    func pluginTitle(_ name: String) -> String? { plugins?.plugin(name)?.title }

    func integrationTitle(_ id: String) -> String? { integrations?.integration(id)?.title }

    func row(_ key: String) -> ConfigRow? { config?.first { $0.key == key } }

    /// `AGENT_BACKEND`: the agent a task runs on when nobody names one.
    var defaultAgent: String? {
        guard case let .string(name)? = row("AGENT_BACKEND")?.value, !name.isEmpty else { return nil }
        return name
    }

    /// The number the owner rings (`TWILIO_NUMBER`), when one is saved.
    var twilioNumber: String? {
        guard case let .string(number)? = row("TWILIO_NUMBER")?.value, !number.isEmpty else { return nil }
        return number
    }
}
