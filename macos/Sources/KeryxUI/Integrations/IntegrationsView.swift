import KeryxKit
import SwiftUI

/// The Integrations section: the accounts Keryx connects to — Google for agents, Gmail,
/// Slack, GitHub and the two admin keys — one card each, connected or not, with what
/// connects it (a secret, a sign-in, a setting) and what disconnects it.
///
/// Connecting a secret is the secret sheet; a Google sign-in is the sign-in sheet;
/// GitHub's issue reports are a setting, turned on after a question since they publish,
/// beside `gh`, which is signed in by hand. Disconnect asks first, then unsets the card's
/// settings and, for the two Google sign-ins, removes their stored tokens.
public struct IntegrationsView: View {
    @Environment(AppModel.self) private var model
    @State private var sheet: IntegrationSheet?
    @State private var asking: Asking?
    /// Cards with a change under way, by id.
    @State private var working: Set<String> = []
    /// What went wrong on each card, by id, said under it.
    @State private var failures: [String: StreamFailure]

    public init() {
        self.init(failures: [:])
    }

    /// `failures`: what each card says went wrong when the screen appears (the catalog's).
    package init(failures: [String: StreamFailure]) {
        _failures = State(initialValue: failures)
    }

    public var body: some View {
        StateView(model.integrations, section: "Integrations",
                  retry: { Task { await model.refresh(.integrations) } }) { report in
            Form {
                ForEach(report.integrations) { integration in
                    IntegrationCard(
                        integration: integration,
                        working: working.contains(integration.id),
                        failure: failures[integration.id],
                        connect: { connect(integration) },
                        change: { secret in sheet = .secret(secret) },
                        disconnect: { ask(.disconnect(integration)) })
                }
            }
            .formStyle(.grouped)
        }
        // What another window asked of Integrations (the setup wizard's Connect Google…,
        // Connect GitHub…, a Google sign-in): answered once the cards are read.
        .onChange(of: model.requested, initial: true) { answerRequest() }
        .onChange(of: model.integrations.value) { answerRequest() }
        .onChange(of: model.integrations.error) { answerRequest() }
        .task {
            await model.refresh(.integrations, .plugins)
            // What GitHub's card and Disconnect read.
            if model.config.value == nil { await model.refresh(.config) }
        }
        .sheet(item: $sheet, onDismiss: reread) { sheet in
            switch sheet {
            case let .secret(secret):
                SecretSheet(key: secret.key, title: secret.title, replacing: secret.set)
            case let .signIn(provider):
                SignInSheet(kind: provider)
            }
        }
        .confirmationDialog(asking?.question.title ?? "", isPresented: Binding(
            get: { asking != nil }, set: { if !$0 { asking = nil } }
        ), presenting: asking) { asking in
            Button(asking.question.button, role: asking.destructive ? .destructive : nil) { answer(asking) }
            Button("Cancel", role: .cancel) {}
        } message: { asking in
            Text(asking.question.message)
        }
    }

    /// What this screen does for `request` (`RequestAnswer`), with the cards as read: a
    /// failed read has no cards to connect, so the section is shown and that is all.
    static func answer(_ request: AppRequest, integrations: Loadable<IntegrationsReport>) -> IntegrationsAnswer? {
        let cards = integrations.value?.integrations ?? (integrations.error == nil ? nil : [])
        return IntegrationsWording.answer(request, cards: cards)
    }

    // MARK: - Actions

    private func answerRequest() {
        guard let request = model.requested, let answer = Self.answer(request, integrations: model.integrations)
        else { return }
        switch answer {
        case .wait: return
        case let .open(requested): sheet = requested
        case let .connect(integration): connect(integration)
        case .unknown: break
        }
        model.didHandle(request)
    }

    private func connect(_ integration: Integration) {
        switch IntegrationsWording.connect(integration) {
        case let .secret(secret): sheet = .secret(secret)
        case let .signIn(provider): sheet = .signIn(provider)
        case .issueReports: ask(.issueReports(integration))
        case nil: break
        }
    }

    private func ask(_ what: Asking.What) {
        asking = Asking(what: what, plugins: model.plugins.value, config: model.config.value)
    }

    private func answer(_ asking: Asking) {
        switch asking.what {
        case let .disconnect(integration):
            change(integration) { await disconnect(integration) }
        case let .issueReports(integration):
            change(integration) {
                await model.set(key: IntegrationsWording.issueReportsKey, to: "true")?
                    .said("Issue reports weren't turned on.")
            }
        }
    }

    /// Runs `work` for the card, with its spinner up; what it returns is the card's failure.
    private func change(_ integration: Integration, _ work: @escaping @MainActor () async -> StreamFailure?) {
        let id = integration.id
        working.insert(id)
        failures[id] = nil
        Task {
            failures[id] = await work()
            await model.refresh(.integrations, .plugins, .tools, .setupPlan)
            working.remove(id)
        }
    }

    /// The card's settings unset in one command, then its sign-in removed; a failure stops it.
    private func disconnect(_ integration: Integration) async -> StreamFailure? {
        let plan = IntegrationsWording.disconnect(integration)
        let sentence = integration.connected ? "\(integration.title) wasn't disconnected."
            : "\(integration.title) wasn't turned off."
        if let error = await model.unset(keys: plan.unset) { return error.said(sentence) }
        if let provider = plan.signOut, case let .failure(error) = await model.signOut(provider) {
            return error.said(sentence)
        }
        return nil
    }

    /// A secret saved or a sign-in finished: the cards, and the plugins that read them.
    private func reread() {
        Task { await model.refresh(.integrations, .plugins, .tools, .setupPlan) }
    }
}

/// The sheet a card opens.
enum IntegrationSheet: Identifiable, Equatable {
    case secret(IntegrationSecret)
    case signIn(SignInProvider)

    var id: String {
        switch self {
        case let .secret(secret): "secret-\(secret.key)"
        case let .signIn(provider): "signin-\(provider.rawValue)"
        }
    }
}

/// A question before a change, with the card it is about.
private struct Asking {
    enum What {
        case disconnect(Integration)
        case issueReports(Integration)
    }

    let what: What
    let question: IntegrationQuestion
    let destructive: Bool

    init(what: What, plugins: PluginsReport?, config: [ConfigRow]?) {
        self.what = what
        switch what {
        case let .disconnect(integration):
            question = IntegrationsWording.disconnectQuestion(integration, plugins: plugins)
            destructive = true
        case .issueReports:
            question = IntegrationsWording.issueReportsQuestion(repo: IntegrationsWording.issueRepo(config))
            destructive = false
        }
    }
}
