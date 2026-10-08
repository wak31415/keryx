import Foundation
import KeryxKit
import Observation

/// One run of the setup wizard: its mode, the frames it has shown (Back's stack), and the
/// few answers a frame hands to a later one before they are settings (where the voice and the
/// work run, the local models chosen, the phone left for later). Everything else is read
/// from the model each time: a frame is a view of the settings and the plan.
@Observable @MainActor
package final class SetupWizard {
    package private(set) var mode: SetupWizardMode
    /// The frames shown, the current one last. Never empty.
    package private(set) var path: [WizardFrame]
    /// The opening this run answers, so a new one starts again.
    package private(set) var opening: UUID

    /// Where the voice runs and who does the work, as chosen on Models this run.
    package var voicePlace: VoicePlace?
    package var workPlace: WorkPlace?
    /// The local model chosen for the work on this Mac, and the voice's own (its "Thinks"
    /// model) — or, with no voice model of its own (today), the model its words come from.
    package var workModel: String?
    package var voiceWordsModel: String?
    /// The voice's words as chosen on Models, from a CLI with a voice LLM of its own: the
    /// agent's model, or a model of its own here; `nil` keeps them as they are.
    package var voiceLLM: VoiceLLMPick?
    /// What the owner stopped this run (a download, a server start): never started again
    /// by itself, only by Start Again.
    package var stopped: Set<LocalStep> = []
    /// In review, the owner clicked to finish what this Mac still needs: Checking may take
    /// the next steps by itself from then on.
    package var localApproved = false
    /// Reaching the end records setup as seen through (`SetupLaunch.completedKey`); the
    /// catalog's runs record nothing.
    package var recordsCompletion = true
    /// Where the voice and the work run and on which models, as last chosen: a new choice
    /// forgets what the owner stopped, which belonged to the old one.
    package func choose(voice: VoicePlace, work: WorkPlace, workModel: String?, voiceWordsModel: String?,
                        voiceLLM: VoiceLLMPick? = nil) {
        if voice != voicePlace || work != workPlace || workModel != self.workModel
            || voiceWordsModel != self.voiceWordsModel || voiceLLM != self.voiceLLM {
            stopped = []
        }
        voicePlace = voice
        workPlace = work
        self.workModel = workModel
        self.voiceWordsModel = voiceWordsModel
        self.voiceLLM = voiceLLM
    }

    package init(mode: SetupWizardMode = .firstRun, path: [WizardFrame] = [.welcome], opening: UUID = UUID()) {
        self.mode = mode
        self.path = path.isEmpty ? [.welcome] : path
        self.opening = opening
    }

    package var current: WizardFrame { path.last ?? .welcome }

    /// Starts again at Welcome for `opening`, unless it is the one this run answers.
    package func start(_ opening: WizardOpening) {
        guard opening.id != self.opening else { return }
        self.opening = opening.id
        mode = opening.mode
        path = [.welcome]
        voicePlace = nil
        workPlace = nil
        workModel = nil
        voiceWordsModel = nil
        voiceLLM = nil
        stopped = []
        localApproved = false
    }

    /// On to the next frame that shows, as things are now; false when there is none.
    @discardableResult
    package func advance(_ facts: WizardFacts) -> Bool {
        guard let next = WizardFlow.next(after: current, mode: mode, facts: facts) else { return false }
        path.append(next)
        return true
    }

    /// Back to the frame shown before; false at Welcome.
    @discardableResult
    package func back() -> Bool {
        guard path.count > 1 else { return false }
        path.removeLast()
        return true
    }

    /// Straight to `frame` (Fix… on Checking), keeping the way back.
    package func go(to frame: WizardFrame) {
        guard frame != current else { return }
        path.append(frame)
    }

    /// The dot strip's place.
    package func position(_ facts: WizardFacts) -> (index: Int, count: Int) {
        WizardFlow.position(path: path, mode: mode, facts: facts)
    }
}
