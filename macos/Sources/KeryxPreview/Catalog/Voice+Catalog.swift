import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Voice screen: each way of speaking in use and picked, the setup on this Mac
    /// running (with sizes and without), stopping, failed and needing a local model first,
    /// a server saved with a warning and one that failed its handshake, and the window.
    @MainActor static var voiceEntries: [Entry] {
        let screen = CGSize(width: 760, height: 720)
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
                VoiceView(mode: .here).environment(settingUp(.holding([downloading, unit])))
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
                    model.setUpVoiceHere()
                    return model
                }())
            },
            Entry("voice-here-finished", size: screen) {
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

    /// A setup on this Mac doing `setup`.
    @MainActor private static func settingUp(_ setup: PreviewClient.Setup) -> AppModel {
        let model = AppModel.preview(client: PreviewClient(setup: setup))
        model.setUpVoiceHere()
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
