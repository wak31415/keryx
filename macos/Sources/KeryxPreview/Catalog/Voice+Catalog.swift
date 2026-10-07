import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Voice screen: each way of speaking in use and picked; the setup on this Mac
    /// running (with sizes and without), stopping, failed, finished and refused; the model
    /// server not running, and Set Up starting it first then going on to the voice (and
    /// failing there); no local model at all; a server saved with a warning and one that
    /// failed its handshake; and the window.
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
                    .error("brew install llama.cpp did not finish"),
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
                    .error("Couldn't download the speech models: the connection to huggingface.co was lost"),
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
