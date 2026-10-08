import Foundation
import KeryxKit

/// What has to happen on this Mac for the voice and the work chosen to run here, one step at
/// a time, read from how things are now: a model downloaded, chosen (`models use`), the
/// speech models downloaded, then the voice set up (which starts the model server first) or
/// the model server started. Models' Continue starts the downloads; Checking takes each next
/// step as the one before it ends, so a window closed part way picks up where it was.
package struct LocalIntent: Equatable, Sendable {
    /// The voice runs on this Mac.
    package var voiceHere: Bool
    /// The work runs on this Mac.
    package var workHere: Bool
    /// The model this Mac's model server must serve: the work's, or — with the work in the
    /// cloud and no voice model of its own — the one the voice takes its words from.
    package var model: String?

    package init(voiceHere: Bool, workHere: Bool, model: String?) {
        self.voiceHere = voiceHere
        self.workHere = workHere
        self.model = model
    }

    /// Nothing runs here.
    package var isEmpty: Bool { !voiceHere && !workHere }

    /// What this run chose on Models, else what is set up now.
    @MainActor package static func of(_ wizard: SetupWizard, facts: WizardFacts, voice: VoiceStatus?,
                                      models: ModelsReport?) -> LocalIntent {
        let voicePlace = wizard.voicePlace ?? VoicePlace.current(voice)
        let workPlace = wizard.workPlace ?? WorkPlace.current(facts, models: models)
        let voiceHere = voicePlace == .thisMac
        let workHere = workPlace == .thisMac
        let model = workHere ? (wizard.workModel ?? models?.servers.llm.model)
            : voiceHere ? (wizard.voiceWordsModel ?? models?.servers.llm.model) : nil
        return LocalIntent(voiceHere: voiceHere, workHere: workHere, model: model)
    }
}

/// The next thing to do for a `LocalIntent`.
package enum LocalStep: Equatable, Sendable {
    /// `models pull NAME`.
    case download(String)
    /// `models use NAME [--default-agent]`.
    case use(String, defaultAgent: Bool)
    /// `voice download`.
    case downloadSpeech
    /// `voice use here`, the model server started first when it is down (`setUpThisMac`).
    case setUpVoice
    /// `models start`.
    case startServer
    /// Something is running; the next step waits for it.
    case wait
    case done

    /// The Checking row it belongs to.
    package var row: CheckRow {
        switch self {
        case .downloadSpeech, .setUpVoice: .voice
        default: .work
        }
    }
}

package enum LocalWork {
    /// The next step, from what is set up now. `busy`: a download, a start or a setup runs.
    package static func next(_ intent: LocalIntent, models: ModelsReport?, voice: VoiceStatus?,
                             facts: WizardFacts, busy: Bool) -> LocalStep {
        guard !intent.isEmpty else { return .done }
        if busy { return .wait }
        if let name = intent.model, let models {
            guard let entry = models.models.first(where: { $0.name == name }) else { return .done }
            if !entry.downloaded { return .download(name) }
            if models.servers.llm.model != name || (intent.workHere && facts.text("AGENT_BACKEND") != "local") {
                return .use(name, defaultAgent: intent.workHere)
            }
        }
        if intent.voiceHere, let voice {
            if !voice.here.modelsReady { return .downloadSpeech }
            if voice.kind != .here || !voice.here.reachable { return .setUpVoice }
        }
        if intent.workHere, let models, !intent.voiceHere,
           !models.servers.llm.reachable || models.servers.llm.servedModel != models.servers.llm.model {
            return .startServer
        }
        return .done
    }
}
