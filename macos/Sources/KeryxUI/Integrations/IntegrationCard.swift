import KeryxKit
import SwiftUI

/// One account on the Integrations screen: what it is, whether it is connected (and if not,
/// why), what connects or disconnects it, and the plugins that read it.
struct IntegrationCard: View {
    let integration: Integration
    let working: Bool
    let failure: String?
    let connect: () -> Void
    /// Replace a secret that is set.
    let change: (IntegrationSecret) -> Void
    let disconnect: () -> Void

    @Environment(AppModel.self) private var model

    var body: some View {
        let config = model.config.value
        Section {
            header(config)
            detail(config)
            if IntegrationsWording.showsUsedBy(integration, plugins: model.plugins.value),
               let usedBy = IntegrationsWording.usedBy(integration.usedBy, plugins: model.plugins.value) {
                LabeledContent("Used by") {
                    Button(usedBy) { model.selection = .plugins }
                        .buttonStyle(.link)
                        .help("Show in Plugins")
                }
            }
            if let failure {
                Label {
                    Text(AppFacing.isFit(failure) ? Wording.sentence(Wording.capitalised(failure))
                        : "Keryx couldn't do that.")
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                }
                .font(.callout)
                if !AppFacing.isFit(failure) { DetailsDisclosure(failure) }
            }
        }
    }

    private func header(_ config: [ConfigRow]?) -> some View {
        HStack(alignment: .top, spacing: 12) {
            IconTile(symbol: IntegrationsWording.symbol(integration.id), tint: IntegrationsWording.tint(integration.id))
            VStack(alignment: .leading, spacing: 2) {
                Text(integration.title)
                    .font(.body.weight(.semibold))
                Text(integration.description)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 16)
            HStack(spacing: 6) {
                StatusDot(IntegrationsWording.tone(integration, config: config))
                Text(IntegrationsWording.status(integration, config: config))
                    .foregroundStyle(.secondary)
            }
            .fixedSize()
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    /// What connects it, how it stands, and the buttons.
    private func detail(_ config: [ConfigRow]?) -> some View {
        LabeledContent {
            HStack(spacing: 8) {
                if working {
                    ProgressView().controlSize(.small)
                }
                buttons(config)
            }
            .disabled(working)
        } label: {
            switch IntegrationsWording.connect(integration) {
            case let .secret(secret):
                // "Key" when the card is named after it already ("OpenAI admin key").
                Text(secret.title.caseInsensitiveCompare(integration.title) == .orderedSame
                    ? "Key" : Wording.capitalised(secret.title))
                Text(secret.set ? "Set" : integration.why ?? "Not set")
            case .signIn:
                Text("Sign-in")
                if integration.connected {
                    Text("Signed in. Disconnect also removes the stored sign-in.")
                } else if let why = integration.why {
                    Text(why)
                }
            case .issueReports:
                Text("Issue reports")
                if let why = integration.why {
                    Text(why)
                } else {
                    Text("Filed with gh on \(IntegrationsWording.issueRepo(config) ?? "Keryx's own repository")")
                }
            case nil:
                Text(integration.why ?? "")
            }
        }
    }

    @ViewBuilder private func buttons(_ config: [ConfigRow]?) -> some View {
        // The buttons agree with the status: Disconnect only for what is connected, Turn Off
        // for what is only turned on.
        let canRemove = IntegrationsWording.canDisconnect(integration, config: config)
        let removal = IntegrationsWording.removal(integration)
        switch IntegrationsWording.connect(integration) {
        case let .secret(secret):
            if secret.set {
                Button(removal, action: disconnect)
                Button("Change…") { change(secret) }
            } else {
                if canRemove { Button(removal, action: disconnect) }
                Button(IntegrationsWording.addSecret(secret), action: connect)
            }
        case .signIn:
            if canRemove { Button(removal, action: disconnect) }
            if IntegrationsWording.needsClientFile(integration) {
                Button("Add Client File…", action: connect)
            } else {
                Button(integration.connected ? "Sign In Again…" : "Sign In…", action: connect)
            }
        case .issueReports:
            if IntegrationsWording.issueReportsOn(config) ?? integration.connected {
                Button("Turn Off…", action: disconnect)
            } else {
                Button("Turn On…", action: connect)
            }
        case nil:
            if canRemove { Button(removal, action: disconnect) }
        }
    }
}
