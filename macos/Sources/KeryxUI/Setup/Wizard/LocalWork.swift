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
    /// The model this Mac's model server must serve: the work's, or — from an older CLI,
    /// with the work in the cloud — the one the voice takes its words from.
    package var model: String?
    /// The voice's words, when Models chose them (`voice llm`); `nil` keeps them.
    package var voiceLLM: VoiceLLMPick?

    package init(voiceHere: Bool, workHere: Bool, model: String?, voiceLLM: VoiceLLMPick? = nil) {
        self.voiceHere = voiceHere
        self.workHere = workHere
        self.model = model
        self.voiceLLM = voiceLLM
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
        // A CLI with a voice LLM of its own needs no agent's model for the voice's words.
        let ownWords = ModelsRules.voiceWordsIsAControl(facts: facts)
        let model = workHere ? (wizard.workModel ?? models?.servers.llm.model)
            : voiceHere && !ownWords ? (wizard.voiceWordsModel ?? models?.servers.llm.model) : nil
        return LocalIntent(voiceHere: voiceHere, workHere: workHere, model: model,
                           voiceLLM: voiceHere && ownWords ? wizard.voiceLLM : nil)
    }
}

/// The next thing to do for a `LocalIntent`.
package enum LocalStep: Hashable, Sendable {
    /// `models pull NAME`.
    case download(String)
    /// `models use NAME [--default-agent]`.
    case use(String, defaultAgent: Bool)
    /// `voice download`.
    case downloadSpeech
    /// `voice llm here NAME`: the voice's own model on a server of its own.
    case voiceLLM(String)
    /// `voice llm shared`: the voice's words from the agent's model.
    case voiceLLMShared
    /// `voice use here`, the model server started first when it is down (`setUpThisMac`).
    case setUpVoice
    /// `models start`.
    case startServer
    /// Something is running; the next step waits for it.
    case wait
    case done

    /// The Checking row it belongs to: the voice's, or the work's — or, for the model the
    /// voice's words come from while the work is in the cloud, the voice's.
    package func row(_ intent: LocalIntent) -> CheckRow {
        switch self {
        case .downloadSpeech, .setUpVoice, .voiceLLM, .voiceLLMShared: .voice
        // The voice's own model downloads for the voice's row.
        case let .download(name): intent.workHere && name == intent.model ? .work : .voice
        case .use: intent.workHere ? .work : .voice
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
            if let step = voiceWords(intent.voiceLLM, models: models, voice: voice, facts: facts) { return step }
            if !voice.here.modelsReady { return .downloadSpeech }
            if voice.kind != .here || !voice.here.reachable { return .setUpVoice }
        }
        // The voice's setup starts the agent's model server only while its words come from it.
        let voiceStartsIt = intent.voiceHere && (voice?.llmSource ?? .shared) == .shared
        if intent.workHere, let models, !voiceStartsIt,
           !models.servers.llm.reachable || models.servers.llm.servedModel != models.servers.llm.model {
            return .startServer
        }
        return .done
    }

    /// The step that brings the voice's words to what was picked, or `nil` once they are.
    /// A model of its own is downloaded first, then served (`voice llm here`); the agent's is
    /// asked for when the words come from elsewhere — or, before the voice is first set up
    /// here, when the CLI's own rule would give it a model of its own instead.
    static func voiceWords(_ pick: VoiceLLMPick?, models: ModelsReport?, voice: VoiceStatus,
                           facts: WizardFacts) -> LocalStep? {
        switch pick {
        case let .own(name)?:
            if let entry = models?.models.first(where: { $0.name == name }), !entry.downloaded { return .download(name) }
            let serving = voice.llmSource == .here && voice.llm?.model == name && voice.llm?.reachable == true
            return serving ? nil : .voiceLLM(name)
        case .shared?:
            if voice.llmSource != .shared { return .voiceLLMShared }
            let unset = facts.row("VOICE_LLM").map { $0.source == "default" } ?? false
            return voice.kind != .here && unset && models?.voiceLlmSeparate == true ? .voiceLLMShared : nil
        case nil:
            return nil
        }
    }
}
