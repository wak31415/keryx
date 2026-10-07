import Foundation
import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Setup Assistant: a new Mac at its first step, each kind of page (settings asked in
    /// place, a sign-in, the PIN with its deferral, a step that waits, a failed optional
    /// step, one to skip, the local models with the model server starting, a migration to
    /// run first, a step already done), the last page ready and not, and the plan failing
    /// to load or Keryx not found.
    @MainActor static var setupEntries: [Entry] {
        let size = CGSize(width: 820, height: 620)
        let fresh = PreviewData.setupPlan
        let partway = PreviewData.setupPlanPartway
        return [
            Entry("setup-first", size: size) {
                SetupAssistantView().environment(assistant(fresh, pin: PreviewData.pinNone))
            },
            Entry("setup-agents", size: size) {
                SetupAssistantView(page: .step("agents")).environment(assistant(partway))
            },
            Entry("setup-owner", size: size) {
                SetupAssistantView(page: .step("owner")).environment(assistant(fresh, pin: PreviewData.pinNone))
            },
            Entry("setup-pin", size: size) {
                SetupAssistantView(page: .step("pin")).environment(assistant(fresh, pin: PreviewData.pinNone))
            },
            Entry("setup-blocked", size: size) {
                SetupAssistantView(page: .step("service")).environment(assistant(fresh, pin: PreviewData.pinNone))
            },
            Entry("setup-failed", size: size) {
                SetupAssistantView(page: .step("plugins")).environment(assistant(partway))
            },
            Entry("setup-optional", size: size) {
                SetupAssistantView(page: .step("issues")).environment(assistant(fresh, pin: PreviewData.pinNone))
            },
            Entry("setup-local", size: size) {
                SetupAssistantView(page: .step("local")).environment(assistant(fresh, models: serverStopped))
            },
            Entry("setup-local-starting", size: size) {
                SetupAssistantView(page: .step("local")).environment({
                    let model = assistant(fresh, models: serverStopped, modelServer: .holding([
                        .step(step: "runtime", message: "Installing llama.cpp with Homebrew"),
                        .progress(step: "runtime", done: 9_437_184, total: 31_457_280),
                    ]))
                    model.runModelServer()
                    return model
                }())
            },
            Entry("setup-migrate", size: size) {
                SetupAssistantView().environment(assistant(migrating))
            },
            Entry("setup-done-step", size: size) {
                SetupAssistantView(page: .step("voice")).environment(assistant(PreviewData.setupPlanReady))
            },
            Entry("setup-ready", size: size) {
                SetupAssistantView().environment(assistant(PreviewData.setupPlanReady, number: "+15550100"))
            },
            Entry("setup-not-ready", size: size) {
                SetupAssistantView(page: .finish).environment(assistant(partway))
            },
            Entry("setup-error", size: CGSize(width: 820, height: 560)) {
                SetupAssistantView().environment(AppModel.preview(client: PreviewClient(setupPlan: .failure(.failed(
                    command: "keryx setup plan", exitCode: 2, message: "No such command 'plan'.")))))
            },
            Entry("setup-not-found", size: CGSize(width: 820, height: 560)) {
                SetupAssistantView().environment(AppModel.preview(location: .failure(LocateFailure(lookedAt: []))))
            },
        ]
    }

    /// A model holding `plan`, with what the buttons read beside it; `number`, a Twilio
    /// number saved.
    @MainActor private static func assistant(
        _ plan: SetupPlan, pin: PinStatus = PreviewData.pinFile, models: ModelsReport = PreviewData.models,
        number: String? = nil, modelServer: PreviewClient.Script<ModelServerStarted> = .replay
    ) -> AppModel {
        var config = PreviewData.config
        // The OpenAI key is saved only once the plan's voice says so.
        if plan.step("voice")?.kind != .done, let index = config.firstIndex(where: { $0.key == "OPENAI_API_KEY" }) {
            config[index].set = false
        }
        if let number, let index = config.firstIndex(where: { $0.key == "TWILIO_NUMBER" }) {
            config[index].value = .string(number)
            config[index].set = true
        }
        return AppModel.preview(client: PreviewClient(
            models: .success(models), config: .success(config), setupPlan: .success(plan), pin: .success(pin),
            modelServer: modelServer))
    }

    /// The local models with a model chosen and downloaded, and its server not running.
    private static var serverStopped: ModelsReport {
        var report = PreviewData.models
        report.servers.llm.reachable = false
        report.servers.llm.servedModel = nil
        return report
    }

    /// A Mac whose old Jarvis files must move first: `migrate` leads, and every other step
    /// waits for it, as `keryx setup plan` has it while the storage refusal stands. Written
    /// by hand from the new-Mac plan: a sandbox would need a `~/.jarvis` of its own.
    private static var migrating: SetupPlan {
        var plan = PreviewData.setupPlan
        let json = """
        {"id": "migrate", "title": "Migration", "required": true, "state": "needed",
         "why": "Storage: /Users/you/.jarvis still holds Keryx's files from when it was Jarvis.",
         "blocked_by": [], "walked": false,
         "checks": [{"title": "Storage", "state": "missing",
                     "detail": "/Users/you/.jarvis still holds Keryx's files from when it was Jarvis — run `keryx migrate`"}]}
        """
        guard let migrate = try? JSON.decoder.decode(SetupStep.self, from: Data(json.utf8)) else {
            fatalError("Setup+Catalog: the migrate step does not decode")
        }
        plan.steps = plan.steps.map { step in
            var waiting = step
            waiting.blockedBy = ["migrate"] + step.blockedBy
            waiting.state = SetupStepState.blocked.rawValue
            waiting.why = "Waits for Migration."
            return waiting
        }
        plan.steps.insert(migrate, at: 0)
        return plan
    }
}
