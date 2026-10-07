import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Configuration screen: its first tab, each tab in turn, the Advanced disclosure
    /// open on a tab of one group and on one of several, a search within one tab and across
    /// several, a write the CLI refused, a change waiting on a restart, the secret sheet, the
    /// option menus, and the whole window around it.
    @MainActor static var configurationEntries: [Entry] {
        let screen = CGSize(width: 760, height: 900)
        return [
            Entry("configuration", size: screen) {
                ConfigurationView().environment(AppModel.preview())
            },
        ] + tabEntries(size: screen) + [
            Entry("configuration-voice-advanced", size: CGSize(width: 760, height: 1180)) {
                ConfigurationView(search: "", tab: "voice", advancedOpen: ["voice"]).environment(AppModel.preview())
            },
            Entry("configuration-system-advanced", size: CGSize(width: 760, height: 1300)) {
                ConfigurationView(search: "", tab: "system", advancedOpen: ["system"]).environment(AppModel.preview())
            },
            Entry("configuration-search", size: CGSize(width: 760, height: 560)) {
                ConfigurationView(search: "vad").environment(AppModel.preview())
            },
            Entry("configuration-search-tabs", size: CGSize(width: 760, height: 1100)) {
                ConfigurationView(search: "key").environment(AppModel.preview())
            },
            Entry("configuration-search-none", size: CGSize(width: 760, height: 460)) {
                ConfigurationView(search: "nothing like it").environment(AppModel.preview())
            },
            Entry("configuration-write-error", size: CGSize(width: 760, height: 1180)) {
                ConfigurationView(search: "", tab: "voice", advancedOpen: ["voice"], errors: ["VAD_SILENCE_MS": refusal])
                    .environment(AppModel.preview(client: PreviewClient(writeError: refusal)))
            },
            Entry("configuration-needs-restart", size: screen) {
                ConfigurationView().environment(needsRestartModel)
            },
            Entry("configuration-secret-sheet", size: CGSize(width: 460, height: 212)) {
                SecretSheet(key: secretRow.key, title: secretRow.displayTitle, replacing: true,
                            typed: "preview-dots-only")
                    .environment(AppModel.preview())
            },
            Entry("configuration-model-menus", size: CGSize(width: 760, height: 640)) {
                ConfigurationView(search: "model").environment(AppModel.preview())
            },
            Entry("configuration-options-unavailable", size: CGSize(width: 760, height: 640)) {
                ConfigurationView(search: "model").environment(AppModel.preview(client: PreviewClient(
                    options: Dictionary(uniqueKeysWithValues: optionKeys.map { ($0, .failure(noOptions)) }))))
            },
            Entry("configuration-other-sheet", size: CGSize(width: 420, height: 210)) {
                OtherValueSheet(title: "Realtime model", current: "gpt-realtime-2.1", choose: { _ in })
                    .environment(AppModel.preview())
            },
            Entry("window-configuration", size: windowSize) {
                RootView().environment(windowModel)
            },
        ]
    }

    /// The settings `config options` can list.
    private static var optionKeys: [String] {
        PreviewData.config.filter(\.hasOptions).map(\.key)
    }

    /// What an older `keryx` says when asked for options it has no command for.
    private static let noOptions = KeryxError.failed(
        command: "keryx config options", exitCode: 2, message: "No such command 'options'.")

    /// The fixture's set secret, OPENAI_API_KEY.
    private static var secretRow: ConfigRow {
        PreviewData.config.first { $0.secret && $0.set } ?? PreviewData.config[0]
    }

    /// What `keryx config set VAD_SILENCE_MS 12oo` says.
    private static let refusal = KeryxError.failed(
        command: "keryx config set", exitCode: 1,
        message: "VAD_SILENCE_MS Input should be a valid integer, unable to parse string as an integer")

    /// One entry per tab, by its id: `configuration-general` … `configuration-system`.
    @MainActor private static func tabEntries(size: CGSize) -> [Entry] {
        var seen: [String] = []
        for row in PreviewData.config.sorted(by: { $0.tabOrder < $1.tabOrder }) where !seen.contains(row.tab) {
            seen.append(row.tab)
        }
        return seen.map { tab in
            Entry("configuration-\(tab)", size: size) {
                ConfigurationView(search: "", tab: tab).environment(AppModel.preview())
            }
        }
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
