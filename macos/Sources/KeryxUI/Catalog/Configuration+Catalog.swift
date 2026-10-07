import KeryxKit
import SwiftUI

extension ScreenCatalog {
    /// The Configuration screen: every setting, a search, a write the CLI refused, a change
    /// waiting on a restart, the secret sheet, and the whole window around it.
    @MainActor static var configurationEntries: [Entry] {
        let screen = CGSize(width: 760, height: 900)
        return [
            Entry("configuration", size: screen) {
                ConfigurationView().environment(AppModel.preview())
            },
            Entry("configuration-lower-groups", size: screen) {
                ConfigurationView().environment(AppModel.preview(client: PreviewClient(config: .success(lowerGroups))))
            },
            Entry("configuration-search", size: CGSize(width: 760, height: 460)) {
                ConfigurationView(search: "vad").environment(AppModel.preview())
            },
            Entry("configuration-write-error", size: screen) {
                ConfigurationView(search: "", errors: ["VAD_SILENCE_MS": refusal])
                    .environment(AppModel.preview(client: PreviewClient(writeError: refusal)))
            },
            Entry("configuration-needs-restart", size: screen) {
                ConfigurationView().environment(needsRestartModel)
            },
            Entry("configuration-secret-sheet", size: CGSize(width: 460, height: 250)) {
                SecretSheet(key: secretRow.key, title: secretRow.displayTitle, description: secretRow.description,
                            replacing: true, typed: "preview-dots-only")
                    .environment(AppModel.preview())
            },
            Entry("window-configuration", size: windowSize) {
                RootView().environment(windowModel)
            },
        ]
    }

    /// The fixture's set secret, OPENAI_API_KEY.
    private static var secretRow: ConfigRow {
        PreviewData.config.first { $0.secret && $0.set } ?? PreviewData.config[0]
    }

    /// What `keryx config set VAD_SILENCE_MS 12oo` says.
    private static let refusal = KeryxError.failed(
        command: "keryx config set", exitCode: 1,
        message: "VAD_SILENCE_MS Input should be a valid integer, unable to parse string as an integer")

    /// The groups below the first screenful, among them a row the environment sets.
    private static var lowerGroups: [ConfigRow] {
        let first = Set(["voice", "agents"])
        return PreviewData.config.filter { !first.contains($0.group) }
    }

    @MainActor private static var needsRestartModel: AppModel {
        let model = AppModel.preview()
        model.configNeedsRestart = true
        return model
    }

    @MainActor private static var windowModel: AppModel {
        let model = AppModel.preview()
        model.selection = .configuration
        return model
    }
}
