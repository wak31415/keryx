import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Local Models screen: a Mac with a model in use and others to download, a
    /// download under way and one that failed, the Use This Model sheet before and after,
    /// a Mac with no local agent yet, the model server stopped, starting (llama.cpp
    /// downloading, then loading), stopping, failed and refused, the speech models
    /// downloading, failed and downloaded, the recommended model shown from Voice, the
    /// Advanced ports open, a list that failed to load, and the whole window around it.
    @MainActor static var modelsEntries: [Entry] {
        let screen = CGSize(width: 720, height: 900)
        // Tall enough for every section down to the catalog's first rows.
        let whole = CGSize(width: 720, height: 1340)
        return [
            Entry("models-populated", size: screen) {
                ModelsView().environment(AppModel.preview())
            },
            Entry("models-downloading", size: CGSize(width: 720, height: 760)) {
                ModelsView().environment(downloading(.holding(done: 4_200_000_000, total: 7_100_000_000)))
            },
            Entry("models-stopping", size: CGSize(width: 720, height: 760)) {
                ModelsView().environment(stopping)
            },
            Entry("models-stale-server", size: CGSize(width: 720, height: 560)) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(models: .success(staleServer))))
            },
            Entry("models-download-failed", size: CGSize(width: 720, height: 760)) {
                ModelsView().environment(downloading(.events([
                    .progress(done: 1_300_000_000, total: 7_100_000_000),
                    .error("The connection to huggingface.co was lost. Try again to pick up where it stopped."),
                ])))
            },
            Entry("models-agent-menu-fallback", size: CGSize(width: 720, height: 560)) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(
                    options: ["LOCAL_AGENT_MODEL": .success(serverDown)])))
            },
            Entry("models-use-sheet", size: CGSize(width: 440, height: 176)) {
                UseModelSheet(entry: gptOss20b).environment(AppModel.preview())
            },
            Entry("models-use-sheet-chosen", size: CGSize(width: 440, height: 176)) {
                UseModelSheet(entry: gptOss20b, stage: .chosen).environment(chosenModel)
            },
            Entry("models-empty-servers", size: CGSize(width: 720, height: 700)) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(
                    models: .success(PreviewData.modelsEmptyServers))))
            },
            Entry("models-server-down", size: whole) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(models: .success(modelServerDown),
                                                                                 options: serverDownOptions)))
            },
            Entry("models-no-llama", size: whole) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(models: .success(noLlama),
                                                                                 options: serverDownOptions)))
            },
            Entry("models-server-installing", size: whole) {
                // No Homebrew: llama.cpp's release build, downloaded with its size known.
                ModelsView().environment(startingServer(.holding([
                    .step(step: "runtime", message: "Installing llama.cpp build b11399, from its releases"),
                    .progress(step: "runtime", done: 9_437_184, total: 31_457_280),
                ]), report: noLlama))
            },
            Entry("models-server-loading", size: whole) {
                ModelsView().environment(startingServer(.holding([
                    .step(step: "runtime", message: "llama.cpp: /opt/homebrew/bin/llama-server"),
                    .step(step: "unit", message: "Installing the model server's service"),
                    .step(step: "start", message: "Loading the model into llama.cpp at http://127.0.0.1:8090/v1"),
                ])))
            },
            Entry("models-server-stopping", size: whole) {
                ModelsView().environment(stoppingServer)
            },
            Entry("models-server-failed", size: whole) {
                ModelsView().environment(startingServer(.events([
                    .step(step: "runtime", message: "llama.cpp is installed.", detail: "/opt/homebrew/bin/llama-server"),
                    .step(step: "unit", message: "Installing the model server's service"),
                    .step(step: "start", message: "Loading the model into llama.cpp at http://127.0.0.1:8090/v1"),
                    .error(StreamFailure("The model server did not come up, so its service was removed.",
                                         detail: "no answer from http://127.0.0.1:8090/v1/models within 600 s; "
                                             + "its log: /Users/you/.local/state/keryx/logs/keryx-llm.err.log")),
                ])))
            },
            Entry("models-server-refused", size: whole) {
                ModelsView().environment(startingServer(.events(
                    PreviewData.modelsStartRefusedLines.compactMap(StreamEvent<ModelServerStarted>.parse))))
            },
            Entry("models-voice-downloading", size: whole) {
                ModelsView().environment(downloadingVoice(.holding([
                    .step(step: "download", message: "Downloading the voice's speech models into the Hugging Face "
                        + "cache: parakeet-tdt to hear, kokoro to speak"),
                    .progress(step: "download", done: 1_363_148_800, total: 2_726_297_600),
                ])))
            },
            Entry("models-voice-download-failed", size: whole) {
                ModelsView().environment(downloadingVoice(.events([
                    .step(step: "download", message: "Downloading the voice's speech models"),
                    .error(StreamFailure("The voice's speech models could not be downloaded.",
                                         detail: "the connection to huggingface.co was lost")),
                ])))
            },
            Entry("models-voice-ready", size: whole) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(voice: .success(PreviewData.voiceHere))))
            },
            Entry("models-voice-models-shown", size: screen) {
                ModelsView().environment({
                    let model = AppModel.preview()
                    model.modelsSpotlight = "voice-models"
                    return model
                }())
            },
            Entry("models-recommended-shown", size: screen) {
                ModelsView().environment({
                    let model = AppModel.preview(client: PreviewClient(models: .success(noAgent),
                                                                       options: serverDownOptions))
                    model.modelsSpotlight = "qwen3-coder-30b-a3b"
                    return model
                }())
            },
            // The whole screen: Advanced is at its foot, under the catalog.
            Entry("models-advanced", size: CGSize(width: 720, height: 1800)) {
                ModelsView(advancedOpen: true).environment(AppModel.preview())
            },
            Entry("models-error", size: CGSize(width: 720, height: 420)) {
                ModelsView().environment(AppModel.preview(client: PreviewClient(
                    models: .failure(.timedOut(command: "keryx models list --json")))))
            },
            Entry("window-models", size: windowSize) {
                RootView().environment(onModels)
            },
        ]
    }

    /// A model downloading Gemma 4 12B, the recommended model, with its pull doing `pull`.
    @MainActor private static func downloading(_ pull: PreviewClient.Pull) -> AppModel {
        let model = AppModel.preview(client: PreviewClient(pull: pull))
        model.download("gemma-4-12b")
        return model
    }

    /// A download asked to stop, still stopping.
    @MainActor private static var stopping: AppModel {
        let model = downloading(.slowToStop(done: 4_200_000_000, total: 7_100_000_000))
        // The first progress event lands on the next turn of the main actor; until then
        // there is nothing running to cancel.
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
        model.downloads.cancel("gemma-4-12b")
        return model
    }

    /// gpt-oss 20B chosen, and the model server still running Qwen3-Coder: the restart that
    /// loads it is still to come.
    private static var staleServer: ModelsReport {
        var report = PreviewData.models
        report.servers.localAgent.model = "gpt-oss-20b"
        report.servers.llm.model = "gpt-oss-20b"
        report.servers.llm.servedModel = "qwen3-coder-30b-a3b"
        return report
    }

    /// The model server not answering, so the agent's menu holds the downloaded models.
    private static var serverDown: ConfigOptions {
        var list = PreviewData.options["LOCAL_AGENT_MODEL"]!
        list.source = "catalog"
        list.live = false
        list.error = "the model server isn't answering at 127.0.0.1:8090"
        list.options = list.options.map { ConfigOption(value: $0.value, label: $0.label, detail: "downloaded") }
        return list
    }

    /// Downloaded and not in use: what Use This Model… is offered on.
    private static var gptOss20b: CatalogModel {
        PreviewData.models.models.first { $0.name == "gpt-oss-20b" }!
    }

    /// Just chosen, with the restart it asks for still to come.
    @MainActor private static var chosenModel: AppModel {
        let model = AppModel.preview()
        model.configNeedsRestart = true
        return model
    }

    /// A model chosen for the model server, which is not running and has no service.
    static var modelServerDown: ModelsReport {
        var report = PreviewData.models
        report.servers.llm.reachable = false
        report.servers.llm.servedModel = nil
        report.servers.llm.unitInstalled = false
        return report
    }

    /// The lists with the model server not answering: the agent's menu holds the
    /// downloaded models, none of them "serving".
    private static var serverDownOptions: [String: Result<ConfigOptions, KeryxError>] {
        var lists = PreviewData.options.mapValues { Result<ConfigOptions, KeryxError>.success($0) }
        lists["LOCAL_AGENT_MODEL"] = .success(serverDown)
        return lists
    }

    /// …and llama.cpp not installed either: what Start Model Server installs first.
    private static var noLlama: ModelsReport {
        var report = modelServerDown
        report.servers.llm.binary = nil
        return report
    }

    /// No local model chosen yet: what Voice's Show Recommended Model leads to.
    private static var noAgent: ModelsReport {
        var report = modelServerDown
        report.servers.llm.model = nil
        report.servers.localAgent.model = nil
        report.servers.localAgent.baseUrl = nil
        report.servers.localAgent.enabled = false
        return report
    }

    /// The model server starting from Local Models, doing `script`.
    @MainActor private static func startingServer(_ script: PreviewClient.Script<ModelServerStarted>,
                                                  report: ModelsReport = modelServerDown) -> AppModel {
        let model = AppModel.preview(client: PreviewClient(models: .success(report), options: serverDownOptions,
                                                           modelServer: script))
        model.runModelServer()
        return model
    }

    /// A start asked to stop, still stopping.
    @MainActor private static var stoppingServer: AppModel {
        let model = startingServer(.slowToStop([
            .step(step: "runtime", message: "llama.cpp: /opt/homebrew/bin/llama-server"),
            .step(step: "unit", message: "Installing the model server's service"),
        ]))
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
        model.modelServerRun.cancel()
        return model
    }

    /// The speech models downloading, doing `script`.
    @MainActor private static func downloadingVoice(_ script: PreviewClient.Script<VoiceModelsDownloaded>) -> AppModel {
        let model = AppModel.preview(client: PreviewClient(voiceDownload: script))
        model.downloadVoiceModels()
        return model
    }

    /// The whole window, on Local Models.
    @MainActor private static var onModels: AppModel {
        let model = AppModel.preview()
        model.selection = .models
        return model
    }
}
