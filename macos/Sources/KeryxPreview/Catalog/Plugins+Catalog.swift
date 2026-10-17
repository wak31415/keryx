import KeryxKit
import KeryxUI
import SwiftUI

extension ScreenCatalog {
    /// The Plugins section and the owner's own tools: the plugins as a sandbox has
    /// them (Slack on, Email answers on and not loading, Billing on and not ready, Cluster
    /// stats off), every account connected with Cluster stats on and a host in its table, a
    /// plugin that would not turn on, the owner's own tools (and none), a list that failed
    /// to load, and the window.
    @MainActor static var pluginsEntries: [Entry] {
        let screen = CGSize(width: 720, height: 1900)
        return [
            Entry("plugins-populated", size: screen) {
                PluginsView().environment(AppModel.preview())
            },
            Entry("plugins-all-on", size: screen) {
                PluginsView().environment(AppModel.preview(client: PreviewClient(
                    integrations: .success(everyAccount), plugins: .success(allOn))))
            },
            Entry("plugins-failure", size: CGSize(width: 720, height: 520)) {
                PluginsView(failures: ["send_to_slack": StreamFailure(
                    "Slack wasn't turned on.", detail: "send_to_slack was not turned on: channel_id is empty (or name "
                        + "an mcp_server whose config has one)")])
                    .environment(AppModel.preview(client: PreviewClient(plugins: .success(slackOff))))
            },
            Entry("plugins-own-tools", size: CGSize(width: 720, height: 560)) {
                ownTools(AppModel.preview())
            },
            Entry("plugins-no-own-tools", size: CGSize(width: 720, height: 340)) {
                ownTools(AppModel.preview(client: PreviewClient(tools: .success(onlyPlugins))))
            },
            Entry("plugins-error", size: CGSize(width: 720, height: 420)) {
                PluginsView().environment(AppModel.preview(client: PreviewClient(
                    plugins: .failure(.timedOut(command: "keryx plugins list --json")))))
            },
            Entry("window-plugins", size: windowSize) {
                RootView().environment(pluginsWindow)
            },
        ]
    }

    /// The owner's own tools on their own, as they sit under the plugins.
    @MainActor private static func ownTools(_ model: AppModel) -> some View {
        Form {
            OwnToolsSection(directory: PreviewData.plugins.directory)
        }
        .formStyle(.grouped)
        .environment(model)
    }

    /// Every account connected.
    private static var everyAccount: IntegrationsReport {
        var report = PreviewData.integrations
        for index in report.integrations.indices {
            report.integrations[index].connected = true
            report.integrations[index].why = nil
        }
        return report
    }

    /// Every plugin on and ready, Cluster stats asking one host.
    private static var allOn: PluginsReport {
        var report = PreviewData.plugins
        for index in report.plugins.indices {
            report.plugins[index].on = true
            report.plugins[index].installed = true
            report.plugins[index].ready = true
            report.plugins[index].why = nil
            report.plugins[index].refused = nil
            for field in report.plugins[index].fields.indices where report.plugins[index].fields[field].key == "clusters" {
                report.plugins[index].fields[field].value = .object(["cluster": .string("gpu")])
            }
        }
        return report
    }

    /// Slack off, its channel not given yet.
    private static var slackOff: PluginsReport {
        var report = PreviewData.plugins
        report.plugins = report.plugins.map { plugin in
            guard plugin.name == "send_to_slack" else { return plugin }
            var plugin = plugin
            plugin.on = false
            plugin.installed = false
            plugin.ready = false
            plugin.why = "Needs the ID of your Slack DM channel."
            for field in plugin.fields.indices { plugin.fields[field].value = .string("") }
            return plugin
        }
        return report
    }

    /// Only the plugins' own tools, and nothing refused.
    private static var onlyPlugins: ToolsReport {
        var report = PreviewData.tools
        report.tools = report.tools.filter { ["check_billing", "send_to_slack"].contains($0.name) }
        report.refused = []
        return report
    }

    /// The whole window, on Plugins.
    @MainActor private static var pluginsWindow: AppModel {
        let model = AppModel.preview()
        model.selection = .plugins
        return model
    }
}
