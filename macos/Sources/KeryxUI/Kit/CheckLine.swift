import KeryxKit
import SwiftUI

/// One of doctor's checks, wherever one is shown (the Overview's Setup section, a Setup
/// Assistant step's "What Keryx Checked"): the doctor's glyph, its title, what was found in
/// a sentence, the button that mends it (its `fix`, through `FixAction`), and — never as the
/// line itself — what a terminal would run or where the thing is on disk, under Show Details
/// ("For the Terminal").
struct CheckLine: View {
    let title: String
    /// `ok`, `missing` or `failed`.
    let state: String
    let detail: String
    var hint: String?
    /// The settings' titles by key, to say a key the detail names by its title.
    var titles: [String: String] = [:]
    /// The fix, as a button; `nil` for none.
    var action: SetupAction?
    var disabled = false
    var perform: (SetupAction) -> Void = { _ in }

    var body: some View {
        let said = CheckWording.detail(detail, titles: titles)
        let terminal = state == "ok" ? said.raw : [said.raw, hint].compactMap { $0 }.joined(separator: "\n")
        LabeledContent {
            if let action, state != "ok" {
                Button(action.title) { perform(action) }
                    .disabled(disabled)
            }
        } label: {
            Label {
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                    if !said.text.isEmpty {
                        Text(said.text)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let terminal, !terminal.isEmpty {
                        DetailsDisclosure(terminal, heading: "For the Terminal")
                    }
                }
            } icon: {
                CheckGlyph.of(state, detail: detail).image
            }
        }
        .padding(.vertical, 2)
    }
}

/// A check's detail as the app says it.
enum CheckWording {
    static let serviceNotInstalled = "The background service isn't installed"

    /// The sentence to show, and what is held back for Show Details: a detail written for a
    /// terminal (a command, a path, a traceback — an older CLI's, before `hint` existed) is
    /// never the line; a phrase for what kind of trouble it is stands in. Code formatting
    /// and a trailing exception name ("(ConnectError)") go, and a setting's key is said by
    /// its title.
    static func detail(_ raw: String, titles: [String: String] = [:]) -> (text: String, raw: String?) {
        var text = raw.trimmingCharacters(in: .whitespaces)
        if let range = text.range(of: #"\s*\([A-Z][A-Za-z]*(Error|Exception|Timeout)\)$"#, options: .regularExpression) {
            text.removeSubrange(range)
        }
        // A service's label ("dev.keryx.agent is not installed — …") is said as what it is;
        // the label is for Show Details.
        if let range = text.range(of: #"^[a-z][a-z0-9-]*(\.[a-z0-9-]+)+ is not installed"#, options: .regularExpression) {
            let rest = text[range.upperBound...]
            return (serviceNotInstalled + rest.replacingOccurrences(of: "`", with: ""), raw)
        }
        guard AppFacing.isFit(text) else {
            // The clause before the terminal's part, when that much is fit to show.
            let clause = text.components(separatedBy: " — ").first ?? ""
            let lead = AppFacing.isFit(clause) && clause != text ? clause : ""
            return (Wording.capitalised(Wording.named(lead, titles: titles)), raw)
        }
        return (Wording.capitalised(Wording.named(text.replacingOccurrences(of: "`", with: ""), titles: titles)), nil)
    }
}

/// Runs a fix's button outside the Setup Assistant (the Overview): a request to another
/// screen, the secret sheet, the service's install after its question, the model server or
/// the speech models started (and shown on Local Models), a trusted page opened.
struct FixPerformer: ViewModifier {
    @Binding var action: SetupAction?

    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL
    @State private var secret: SecretRequest?
    @State private var confirmingInstall = false
    @State private var failure: KeryxError?

    func body(content: Content) -> some View {
        content
            .onChange(of: action) { _, requested in
                guard let requested else { return }
                action = nil
                run(requested)
            }
            .sheet(item: $secret, onDismiss: { Task { await model.refresh(.doctor, .setupPlan, .config) } }) { request in
                SecretSheet(key: request.key, title: request.title, replacing: request.replacing)
            }
            .confirmationDialog(ServiceWording.Change.install.question, isPresented: $confirmingInstall) {
                Button(ServiceWording.Change.install.button) { install() }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(ServiceWording.Change.install.message(assistant: Wording.assistant(model.status.value)
                    ?? "the assistant", liveCalls: 0))
            }
            .errorAlert($failure)
    }

    private func run(_ action: SetupAction) {
        switch action.effect {
        case let .request(request):
            model.request(request)
        case let .secret(key, title, replacing):
            secret = SecretRequest(key: key, title: title, replacing: replacing)
        case .installService:
            confirmingInstall = true
        case .startModelServer:
            model.runModelServer()
            model.show(inModels: ModelsAnchor.modelServer)
        case .downloadVoiceModels:
            model.downloadVoiceModels()
            model.show(inModels: ModelsAnchor.voiceModels)
        case let .openURL(url):
            openURL(url)
        case .mark, .go, .terminal:
            // The Setup Assistant's own: not offered outside it.
            break
        }
    }

    private func install() {
        Task {
            if case let .failure(error) = await model.installService() { failure = error }
        }
    }
}

/// The secret sheet's subject.
struct SecretRequest: Identifiable {
    let key: String
    let title: String
    let replacing: Bool

    var id: String { key }
}

extension View {
    /// Runs the fix put in `action` (and clears it).
    func performsFixes(_ action: Binding<SetupAction?>) -> some View {
        modifier(FixPerformer(action: action))
    }
}
