import KeryxKit
import SwiftUI

/// One plugin: a switch that turns it on or off (`plugins install` / `plugins remove`),
/// whether it works and why not, the account it needs first, and its settings by type.
///
/// A plugin that is on saves each setting as it is committed (`plugins set`). One that is
/// off keeps them in its draft until it is turned on, when they go with the install.
struct PluginCard: View {
    let plugin: Plugin
    let working: Bool
    let failure: String?
    @Binding var draft: PluginDraft
    let turnOn: () -> Void
    let askOff: () -> Void
    let save: (PluginValues) -> Void
    /// Asked for by another window: tinted a moment.
    var marked = false

    @Environment(AppModel.self) private var model
    @State private var problems: [String: String] = [:]
    @State private var showsMore = false

    var body: some View {
        let missing = PluginsWording.missingAccounts(plugin, integrations: model.integrations.value)
        let fields = PluginsWording.split(plugin.fields)
        Section {
            Toggle(isOn: Binding(get: { plugin.installed }, set: { $0 ? turnOn() : askOff() })) {
                header
            }
            .toggleStyle(.switch)
            .background {
                RoundedRectangle(cornerRadius: 6)
                    .fill(Color.accentColor.opacity(marked ? 0.14 : 0))
                    .padding(.horizontal, -6)
                    .padding(.vertical, -2)
            }
            .disabled(working || (!plugin.installed && !missing.isEmpty))
            status(missing)
            ForEach(fields.important) { row($0) }
            if !fields.more.isEmpty {
                DisclosureGroup("Advanced", isExpanded: $showsMore) {
                    ForEach(fields.more) { row($0) }
                }
            }
            if let failure {
                Text(PluginsWording.failure(failure, plugins: model.plugins.value))
                    .font(.callout)
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } footer: {
            if !plugin.installed, !plugin.fields.isEmpty {
                Text(draft.isEmpty ? "Its settings are saved when you turn it on."
                    : "Your changes are saved when you turn it on.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            IconTile(symbol: PluginsWording.symbol(plugin.name), tint: PluginsWording.tint(plugin.name))
            VStack(alignment: .leading, spacing: 2) {
                Text(plugin.title)
                    .font(.body.weight(.semibold))
                Text(plugin.description)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 2)
    }

    private func status(_ missing: [Integration]) -> some View {
        let status = PluginsWording.status(plugin)
        return LabeledContent {
            HStack(spacing: 8) {
                if working {
                    ProgressView().controlSize(.small)
                }
                if let title = PluginsWording.connectFirst(missing) {
                    // Straight to the account's own Connect, on Integrations.
                    Button(title) {
                        if let first = missing.first { model.request(.connect(integration: first.id)) }
                    }
                }
            }
        } label: {
            HStack(spacing: 6) {
                StatusDot(status.tone)
                Text(status.text)
            }
            if let detail = PluginsWording.detail(plugin, assistant: Wording.assistant(model.status.value)) {
                Text(detail)
            }
        }
    }

    @ViewBuilder private func row(_ field: PluginField) -> some View {
        if field.kind == .map {
            ClusterTableRows(field: field, table: draft.clusters ?? PluginsWording.table(field.value),
                             hosts: model.sshHosts.value, keepsOne: plugin.installed, disabled: working) { table in
                if plugin.installed {
                    save(PluginValues(clusters: table))
                } else {
                    draft.clusters = table
                }
            }
        } else {
            PluginFieldRow(field: field, value: draft.settings[field.key] ?? PluginsWording.text(field.value),
                           problem: problems[field.key], disabled: working) { commit(field, $0) }
        }
    }

    private func commit(_ field: PluginField, _ typed: String) {
        switch PluginsWording.parse(typed, as: field.kind) {
        case let .failure(problem):
            problems[field.key] = problem.message
        case let .success(value):
            problems[field.key] = nil
            if plugin.installed {
                guard value != PluginsWording.text(field.value) else { return }
                save(PluginValues(settings: [field.key: value]))
            } else {
                draft.settings[field.key] = value == PluginsWording.text(field.value) ? nil : value
            }
        }
    }
}
