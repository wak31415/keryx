import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Voice screen: each way of speaking in use and picked; the setup on this Mac
    /// running (with sizes and without), stopping, failed, finished and refused; the model
    /// server not running, and Set Up starting it first then going on to the voice (and
    /// failing there); no local model at all; a server saved with a warning and one that
    /// failed its handshake; This Mac with Advanced open; and the window.
    @MainActor static var voiceEntries: [Entry] {
        let screen = CGSize(width: 760, height: 720)
        // Room for every step of a setup that starts the model server first.
        let tall = CGSize(width: 760, height: 1040)
        return [
            Entry("voice-openai", size: screen) {
                VoiceView().environment(AppModel.preview())
            },
            Entry("voice-openai-no-key", size: screen) {
                VoiceView().environment(AppModel.preview(client: PreviewClient(
                    voice: .success(noKey), options: noKeyOptions)))
            },
            Entry("voice-here", size: screen) {
                VoiceView().environment(AppModel.preview(client: PreviewClient(voice: .success(PreviewData.voiceHere))))
            },
            Entry("voice-here-picked", size: screen) {
                VoiceView(mode: .here).environment(AppModel.preview())
            },
            Entry("voice-here-advanced", size: tall) {
                VoiceView(mode: .here, advancedOpen: true).environment(AppModel.preview())
            },
            Entry("voice-here-setting-up", size: screen) {
                VoiceView(mode: .here).environment(settingUp(.holding([downloading])))
            },
            Entry("voice-here-setting-up-step", size: screen) {
                VoiceView(mode: .here).environment(settingUp(.holding([downloading, speech, configure, unit])))
            },
            Entry("voice-here-downloading", size: tall) {
                VoiceView(mode: .here).environment(settingUp(.holding([downloading, speech, speechHalf])))
            },
            Entry("voice-here-server-down", size: screen) {
                VoiceView(mode: .here).environment(AppModel.preview(client: PreviewClient(
                    models: .success(modelServerDown))))
            },
            Entry("voice-here-chain-server", size: tall) {
                VoiceView(mode: .here).environment(chain(server: .holding([
                    .step(step: "runtime", message: "Installing llama.cpp with Homebrew"),
                    .progress(step: "runtime", done: 9_437_184, total: nil),
                ])))
            },
            Entry("voice-here-chain-voice", size: tall) {
                VoiceView(mode: .here).environment(chain(server: .replay,
                                                         setup: .holding([downloading, speech, speechHalf])))
            },
            Entry("voice-here-chain-failed", size: tall) {
                VoiceView(mode: .here).environment(chain(server: .events([
                    .step(step: "runtime", message: "Installing llama.cpp with Homebrew"),
                    .error(StreamFailure("llama.cpp could not be installed.",
                                         detail: "brew install llama.cpp exited with 1")),
                ])))
            },
            Entry("voice-here-ollama-down", size: screen) {
                VoiceView(mode: .here).environment(refusedWords(base: "http://127.0.0.1:11434/v1"))
            },
            Entry("voice-here-elsewhere-down", size: screen) {
                VoiceView(mode: .here).environment(refusedWords(base: "http://studio.local:8080/v1"))
            },
            Entry("voice-here-server-starting-in-models", size: screen) {
                VoiceView(mode: .here).environment({
                    let model = AppModel.preview(client: PreviewClient(
                        models: .success(modelServerDown),
                        modelServer: .holding([.step(step: "start", message: "Loading the model")])))
                    model.runModelServer()
                    return model
                }())
            },
            Entry("voice-here-server-refused", size: screen) {
                VoiceView(mode: .here).environment({
                    let model = AppModel.preview(client: PreviewClient(
                        setup: .events([.error("The local model server isn't running: start it first.")])))
                    model.setUpThisMac(startServer: false)
                    return model
                }())
            },
            Entry("voice-here-stopping", size: screen) {
                VoiceView(mode: .here).environment(stopping)
            },
            Entry("voice-here-failed", size: screen) {
                VoiceView(mode: .here).environment(settingUp(.events([
                    downloading,
                    .error(StreamFailure("The voice's speech models could not be downloaded.",
                                         detail: "the connection to huggingface.co was lost")),
                ])))
            },
            Entry("voice-here-refused", size: screen) {
                VoiceView(mode: .here).environment({
                    let model = AppModel.preview(client: PreviewClient(
                        voice: .success(PreviewData.voiceNeedsModel),
                        setup: .events(PreviewData.voiceHereRefusedLines.compactMap(VoiceSetupEvent.parse))))
                    model.setUpThisMac()
                    return model
                }())
            },
            Entry("voice-here-finished", size: tall) {
                VoiceView(mode: .here).environment(settingUp(.replay))
            },
            Entry("voice-here-needs-model", size: screen) {
                VoiceView(mode: .here).environment(AppModel.preview(client: PreviewClient(
                    voice: .success(PreviewData.voiceNeedsModel))))
            },
            // The voice LLM: a model of its own in use; each source picked; one being chosen,
            // chosen, refused over a live call, and failed; and no model named for it yet.
            Entry("voice-here-own-llm", size: tall) {
                VoiceView().environment(AppModel.preview(client: PreviewClient(models: .success(ownLLMModels),
                                                                               voice: .success(PreviewData.voiceOwnLLM))))
            },
            Entry("voice-here-llm-own-picked", size: tall) {
                VoiceView(mode: .here, llm: .here).environment(AppModel.preview(client: PreviewClient(
                    models: .success(separateModels))))
            },
            Entry("voice-here-llm-remote-picked", size: tall) {
                VoiceView(mode: .here, llm: .remote).environment(AppModel.preview())
            },
            Entry("voice-here-llm-choosing", size: tall) {
                VoiceView(mode: .here, llm: .here).environment(choosingLLM(.holding(Array(
                    PreviewData.voiceLLMHereLines.prefix(2).compactMap(VoiceLLMEvent.parse)))))
            },
            Entry("voice-here-llm-chosen", size: tall) {
                VoiceView(mode: .here, llm: .here).environment(choosingLLM(.replay))
            },
            Entry("voice-here-llm-live-call", size: tall) {
                VoiceView(mode: .here, llm: .here).environment(choosingLLM(.events([
                    .error(StreamFailure("A call is going on, and switching the voice's words restarts the voice "
                        + "server, which would cut it off: try again once the line is clear.", detail: "1 live call(s)")),
                ])))
            },
            Entry("voice-here-llm-failed", size: tall) {
                VoiceView(mode: .here, llm: .here).environment(choosingLLM(.events([
                    .step(step: "download", message: "Downloading Qwen3.5 4B for the voice LLM (2.7 GB)"),
                    .step(step: "runtime", message: "llama.cpp is installed.", detail: "/opt/homebrew/bin/llama-server"),
                    .step(step: "unit", message: "Installing the voice LLM server's service, for Qwen3.5 4B"),
                    .step(step: "start", message: "Loading Qwen3.5 4B into llama.cpp at http://127.0.0.1:8091/v1"),
                    .error(StreamFailure("The voice LLM server did not come up, so its service was removed.",
                                         detail: "no answer from http://127.0.0.1:8091/v1/models within 600 s; "
                                             + "its log: /Users/you/.local/state/keryx/logs/keryx-voice-llm.err.log")),
                ])))
            },
            Entry("voice-here-llm-stopping", size: tall) {
                VoiceView(mode: .here, llm: .here).environment({
                    let model = choosingLLM(.slowToStop(Array(PreviewData.voiceLLMHereLines.prefix(2)
                        .compactMap(VoiceLLMEvent.parse))))
                    RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
                    model.voiceLLMRun.cancel()
                    return model
                }())
            },
            // The CLI's per-row fit as the voice's LLM (not printed yet), put in by hand.
            Entry("voice-here-llm-fits", size: tall) {
                VoiceView(mode: .here, llm: .here).environment(AppModel.preview(client: PreviewClient(
                    models: .success(fitsAsVoice))))
            },
            Entry("voice-here-needs-voice-llm", size: tall) {
                VoiceView().environment(AppModel.preview(client: PreviewClient(voice: .success(needsVoiceLLM))))
            },
            Entry("voice-here-live-call", size: screen) {
                VoiceView(mode: .here).environment(settingUp(.events([
                    .error(StreamFailure("A call is going on, and setting up the voice here restarts its server, "
                        + "which would cut it off: try again once the line is clear.", detail: "2 live call(s)")),
                ])))
            },
            Entry("voice-server", size: screen) {
                VoiceView().environment(AppModel.preview(client: PreviewClient(voice: .success(PreviewData.voiceServer))))
            },
            Entry("voice-server-picked", size: screen) {
                VoiceView(mode: .server).environment(AppModel.preview())
            },
            Entry("voice-server-stored-key", size: screen) {
                VoiceView().environment(storedKey)
            },
            Entry("voice-server-removing-key", size: screen) {
                VoiceView(mode: .server, server: .init(clearing: true)).environment(storedKey)
            },
            Entry("voice-server-saved-warning", size: screen) {
                VoiceView(mode: .server, server: .init(address: "http://studio.local:8000/v1",
                                                       answer: PreviewClient.serverSaved))
                    .environment(needsRestart)
            },
            Entry("voice-server-problem", size: screen) {
                VoiceView(mode: .server, server: .init(address: "http://studio.local:8000/v1", keyTyped: true,
                                                       answer: PreviewClient.serverProblem))
                    .environment(AppModel.preview())
            },
            Entry("voice-error", size: CGSize(width: 760, height: 420)) {
                VoiceView().environment(AppModel.preview(client: PreviewClient(voice: .failure(.failed(
                    command: "keryx voice", exitCode: 2, message: "No such command 'voice'.")))))
            },
            Entry("window-voice", size: windowSize) {
                RootView().environment(onVoice)
            },
        ]
    }

    /// The models beside a voice LLM of its own: the 4b chosen and downloaded (`models list
    /// --json`'s keys, as a 36 GB Mac with the 4b chosen gives them).
    private static var ownLLMModels: ModelsReport {
        var report = PreviewData.models
        report.voiceLlmRecommended = "qwen3.5-4b"
        report.voiceLlmSeparate = true
        report.models = report.models.map { entry in
            var entry = entry
            if entry.name == "qwen3.5-4b" { entry.downloaded = true }
            return entry
        }
        return report
    }

    /// A Mac where the owner's rule gives the voice a model of its own: the 4b recommended.
    private static var separateModels: ModelsReport {
        var report = PreviewData.models
        report.voiceLlmRecommended = "qwen3.5-4b"
        report.voiceLlmSeparate = true
        return report
    }

    /// `separateModels` with `fits_as_voice_llm` as the CLI is to print it, written by hand: on
    /// this 36 GB Mac beside Qwen3-Coder, the 4b fits as the voice's LLM, the 9b does not.
    private static var fitsAsVoice: ModelsReport {
        var report = separateModels
        report.models = report.models.map { entry in
            var entry = entry
            entry.fitsAsVoiceLlm = entry.name == "qwen3.5-4b"
            return entry
        }
        return report
    }

    /// The voice here with an LLM of its own and no model named for it.
    private static var needsVoiceLLM: VoiceStatus {
        var voice = PreviewData.voiceOwnLLM
        voice.needs = VoiceStatus.voiceLLM
        voice.llm = VoiceLLMStatus(source: "here", model: nil, url: "http://127.0.0.1:8091/v1", reachable: nil,
                                   unitInstalled: false)
        voice.here.wordsModel = nil
        voice.here.reachable = false
        return voice
    }

    /// The voice LLM being chosen, as `script` goes, with the 4b recommended.
    @MainActor private static func choosingLLM(_ script: PreviewClient.Script<VoiceLLMChosen>) -> AppModel {
        let model = AppModel.preview(client: PreviewClient(models: .success(separateModels), voiceLLM: script))
        model.chooseVoiceLLM(.here(model: "qwen3.5-4b"))
        return model
    }

    /// OpenAI Realtime in use with no key to use it with.
    private static var noKey: VoiceStatus {
        var voice = PreviewData.voice
        voice.openaiKeySet = false
        return voice
    }

    /// The lists with no OpenAI key: the Realtime models fall back to the built-in one.
    private static var noKeyOptions: [String: Result<ConfigOptions, KeryxError>] {
        var lists = PreviewData.options.mapValues { Result<ConfigOptions, KeryxError>.success($0) }
        lists["OPENAI_REALTIME_MODEL"] = .success(PreviewData.realtimeFallback)
        return lists
    }

    /// uv reports no sizes, so an install step's bar is indeterminate.
    private static let downloading = VoiceSetupEvent.step(
        step: "install", message: "Installing the voice server", progress: nil)

    private static let unit = VoiceSetupEvent.step(
        step: "unit", message: "Installing the voice server's service", progress: nil)

    private static let speech = VoiceSetupEvent.step(
        step: "download", message: "Downloading the voice's speech models into the Hugging Face cache: "
            + "parakeet-tdt to hear, kokoro to speak", progress: nil)

    private static let speechHalf = VoiceSetupEvent.progress(step: "download", done: 1_363_148_800,
                                                             total: 2_726_297_600)

    private static let configure = VoiceSetupEvent.step(
        step: "configure", message: "Saving the voice settings", progress: nil)

    /// Set Up… on This Mac starting the model server first, which does `server`; the voice
    /// setup that follows does `setup`.
    /// The voice refused because the words' server isn't answering, the local agent's model
    /// being served at `base` rather than by this Mac's model server.
    @MainActor private static func refusedWords(base: String) -> AppModel {
        var report = PreviewData.models
        report.servers.llm.model = nil
        report.servers.llm.reachable = false
        report.servers.localAgent.baseUrl = base
        let model = AppModel.preview(client: PreviewClient(
            models: .success(report), setup: .events([.error("The local model server isn't running: start it first.")])))
        model.setUpThisMac()
        return model
    }

    /// The models say the model server is down until it has started (the voice's part
    /// reads them again once it has).
    @MainActor private static func chain(server: PreviewClient.Script<ModelServerStarted>,
                                         setup: PreviewClient.Setup = .replay) -> AppModel {
        let started: Bool
        if case .replay = server { started = true } else { started = false }
        let model = AppModel.preview(client: PreviewClient(models: .success(started ? PreviewData.models : modelServerDown),
                                                           setup: setup, modelServer: server))
        model.setUpThisMac(startServer: true)
        return model
    }

    /// A setup on this Mac doing `setup`, with the model server already running.
    @MainActor private static func settingUp(_ setup: PreviewClient.Setup) -> AppModel {
        let model = AppModel.preview(client: PreviewClient(setup: setup))
        model.setUpThisMac()
        return model
    }

    /// A setup asked to stop, still stopping.
    @MainActor private static var stopping: AppModel {
        let model = settingUp(.slowToStop([downloading]))
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
        model.voiceSetup.cancel()
        return model
    }

    /// A server in use with its API key stored.
    @MainActor private static var storedKey: AppModel {
        let rows = PreviewData.config.map { row in
            var row = row
            if row.key == "VOICE_API_KEY" {
                row.set = true
                row.source = "secrets.toml"
            }
            return row
        }
        return AppModel.preview(client: PreviewClient(config: .success(rows), voice: .success(PreviewData.voiceServer)))
    }

    @MainActor private static var needsRestart: AppModel {
        let model = AppModel.preview(client: PreviewClient(voice: .success(PreviewData.voiceServer)))
        model.configNeedsRestart = true
        return model
    }

    @MainActor private static var onVoice: AppModel {
        let model = AppModel.preview()
        model.selection = .voice
        return model
    }
}
