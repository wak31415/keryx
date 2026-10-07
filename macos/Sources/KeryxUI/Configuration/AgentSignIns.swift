import KeryxKit
import SwiftUI

/// The top of the Agents tab: Claude's and Codex's sign-ins, each with whether it is signed
/// in and the button that opens its sign-in sheet (`keryx auth login NAME`).
struct AgentSignInSection: View {
    let open: (SignInProvider) -> Void

    @Environment(AppModel.self) private var model

    var body: some View {
        Section("Sign-ins") {
            ForEach(AgentSignInWording.providers) { provider in
                let state = AgentSignInWording.state(provider, doctor: model.doctor.value)
                LabeledContent {
                    HStack(spacing: 8) {
                        Text(AgentSignInWording.status(state))
                            .foregroundStyle(.secondary)
                        Button(AgentSignInWording.button(state)) { open(provider) }
                    }
                } label: {
                    Text(SignInWording.name(provider))
                    if let caption = AgentSignInWording.caption(state) {
                        Text(caption)
                    }
                }
            }
        }
    }
}

/// Whether a coding agent is signed in, as `keryx auth status` reads it: from the doctor's
/// check for that agent (`Claude Code agent`, `Codex agent`, " (default)" after the
/// default's), which an agent that is not enabled has none of. Its detail is the credential
/// when it is ready ("stored subscription login"), else "not installed", "the claude program
/// is missing" or "not signed in".
enum AgentSignInWording {
    enum State: Equatable {
        /// Ready, and on what: the credential the doctor names ("stored subscription login").
        case signedIn(String?)
        case notSignedIn
        /// Its SDK, or the program it runs, is not installed.
        case notInstalled
        /// Not among the agents enabled, so the doctor does not check it.
        case notEnabled
        /// The doctor has not been read yet.
        case unknown
    }

    /// The agents signed in to from the Agents tab.
    static let providers: [SignInProvider] = [.claude, .codex]

    /// The doctor check's name each agent's starts with (`auth status`'s prefixes).
    static func checkPrefix(_ provider: SignInProvider) -> String? {
        switch provider {
        case .claude: "Claude Code agent"
        case .codex: "Codex agent"
        case .gmail, .googleWorkspace: nil
        }
    }

    static func state(_ provider: SignInProvider, doctor: DoctorReport?) -> State {
        guard let doctor else { return .unknown }
        guard let prefix = checkPrefix(provider),
              let check = doctor.checks.first(where: { $0.name.hasPrefix(prefix) }) else { return .notEnabled }
        let detail = check.detail.trimmingCharacters(in: .whitespaces)
        if check.ok {
            // The credential; after "; " when a CLI from before put where the program is first.
            let how = detail.range(of: "; ").map { String(detail[$0.upperBound...]) } ?? detail
            return .signedIn(how.isEmpty ? nil : how)
        }
        let missing = detail.hasPrefix("not installed") || detail.hasSuffix("program is missing")
            || detail.contains("CLI not found")
        return missing ? .notInstalled : .notSignedIn
    }

    static func status(_ state: State) -> String {
        switch state {
        case .signedIn: "Signed In"
        case .notSignedIn: "Not Signed In"
        case .notInstalled: "Not Installed"
        case .notEnabled: "Not Enabled"
        case .unknown: "Checking…"
        }
    }

    static func button(_ state: State) -> String {
        if case .signedIn = state { return "Sign In Again…" }
        return "Sign In…"
    }

    /// Under the agent's name: the credential it runs on ("Stored subscription login"), or
    /// why it can't run yet.
    static func caption(_ state: State) -> String? {
        switch state {
        case let .signedIn(how): how.map(Wording.capitalised)
        case .notSignedIn: "Its tasks can't run until it is signed in."
        case .notInstalled: "It isn't installed on this Mac."
        case .notEnabled: "Not among the agents Keryx may use."
        case .unknown: nil
        }
    }
}
