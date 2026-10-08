import KeryxKit
import SwiftUI

/// The top of the Agents tab: Claude's and Codex's sign-ins, each with whether it is signed
/// in and the button that opens its sign-in sheet (`keryx auth login NAME`). An agent that is
/// not enabled offers Enable instead — it joins `AGENTS_ENABLED`, written as the checklist
/// below writes it — and one that is not installed offers nothing.
struct AgentSignInSection: View {
    let open: (SignInProvider) -> Void

    @Environment(AppModel.self) private var model
    @State private var enabling: SignInProvider?
    @State private var failures: [SignInProvider: KeryxError] = [:]

    var body: some View {
        Section("Sign-ins") {
            ForEach(AgentSignInWording.providers) { provider in
                let state = AgentSignInWording.state(provider, doctor: model.doctor.value)
                LabeledContent {
                    HStack(spacing: 8) {
                        if enabling == provider {
                            ProgressView().controlSize(.small)
                        }
                        Text(AgentSignInWording.status(state))
                            .foregroundStyle(.secondary)
                        if state == .notEnabled {
                            Button(AgentSignInWording.enable) { enable(provider) }
                                .disabled(enabling != nil || enabledRow == nil)
                        } else if let title = AgentSignInWording.button(state) {
                            Button(title) { open(provider) }
                        }
                    }
                } label: {
                    Text(SignInWording.name(provider))
                    if let caption = AgentSignInWording.caption(state) {
                        Text(caption)
                    }
                    if let error = failures[provider] {
                        ErrorText(error.said(AgentSignInWording.notEnabled(provider)))
                    }
                }
            }
        }
    }

    /// `AGENTS_ENABLED`, once the settings are read.
    private var enabledRow: ConfigRow? {
        model.config.value?.first { $0.key == AgentSignInWording.enabledKey }
    }

    /// Adds the agent to `AGENTS_ENABLED` — blank meaning the default agent alone, which
    /// stays — then reads the doctor again, whose check for it says whether it is signed in.
    private func enable(_ provider: SignInProvider) {
        guard let row = enabledRow, enabling == nil else { return }
        let fallback = model.config.value?.first { $0.key == "AGENT_BACKEND" }.map { ConfigWording.editText($0.value) }
        let value = AgentSignInWording.enabling(provider, row: row, fallback: fallback)
        enabling = provider
        failures[provider] = nil
        Task {
            if let error = await model.set(row, to: value) {
                failures[provider] = error
            } else {
                await model.refresh(.doctor)
            }
            enabling = nil
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

    /// The sign-in's fix id (`signin:claude`): the doctor's own word that it needs one.
    static func fixId(_ provider: SignInProvider) -> String { "signin:" + provider.rawValue }

    /// Read first from the doctor's fix ids — a check that offers this sign-in is one that is
    /// not signed in — then from the agent's check: ready, or (offering no fix) not
    /// installed. A CLI from before fix ids is read from the check's detail.
    static func state(_ provider: SignInProvider, doctor: DoctorReport?) -> State {
        guard let doctor else { return .unknown }
        if doctor.checks.contains(where: { $0.fix == fixId(provider) }) { return .notSignedIn }
        guard let prefix = checkPrefix(provider),
              let check = doctor.checks.first(where: { $0.name.hasPrefix(prefix) }) else { return .notEnabled }
        let detail = check.detail.trimmingCharacters(in: .whitespaces)
        if check.ok {
            // The credential; after "; " when a CLI from before put where the program is first.
            let how = detail.range(of: "; ").map { String(detail[$0.upperBound...]) } ?? detail
            return .signedIn(how.isEmpty ? nil : how)
        }
        // A doctor that gives fix ids offers a sign-in for every agent that wants one: one
        // that is not ready and offers none is not installed.
        if speaksFixIds(doctor) { return .notInstalled }
        let missing = detail.hasPrefix("not installed") || detail.hasSuffix("program is missing")
            || detail.contains("CLI not found")
        return missing ? .notInstalled : .notSignedIn
    }

    /// The doctor is one that gives each check a `fix` (and `hint`): some check carries one.
    static func speaksFixIds(_ doctor: DoctorReport) -> Bool {
        doctor.checks.contains { $0.fix != nil || $0.hint != nil }
    }

    static func status(_ state: State) -> String {
        switch state {
        case .signedIn: "Signed in"
        case .notSignedIn: "Not signed in"
        case .notInstalled: "Not installed"
        case .notEnabled: "Not enabled"
        case .unknown: "Checking…"
        }
    }

    /// The sign-in's button; `nil` where signing in is not what is missing — the agent is
    /// not installed or not enabled (`enable`) — or the doctor is not read yet.
    static func button(_ state: State) -> String? {
        switch state {
        case .signedIn: "Sign In Again…"
        case .notSignedIn: "Sign In…"
        case .notInstalled, .notEnabled, .unknown: nil
        }
    }

    /// The button an agent that is not enabled offers instead.
    static let enable = "Enable"

    static let enabledKey = "AGENTS_ENABLED"

    /// "Codex wasn't enabled."
    static func notEnabled(_ provider: SignInProvider) -> String {
        "\(SignInWording.name(provider)) wasn't enabled."
    }

    /// `AGENTS_ENABLED` with `provider` added, as the checklist writes it
    /// (`ConfigWording.toggling`); `fallback`, the default agent, which a blank list means.
    static func enabling(_ provider: SignInProvider, row: ConfigRow, fallback: String?) -> String {
        let on = ConfigWording.checked(row.value, fallback: fallback)
        return ConfigWording.toggling(provider.rawValue, on: true, in: on, choices: row.choices ?? [])
    }

    /// Under the agent's name: the credential it runs on ("Stored subscription login"), or
    /// why it can't run yet.
    static func caption(_ state: State) -> String? {
        switch state {
        case let .signedIn(how): how.map(Wording.capitalised)
        case .notSignedIn: "Its tasks can't run until it is signed in."
        case .notInstalled: "It isn't installed on this Mac."
        case .notEnabled: "Not among the agents Keryx may use. Enable it to sign in."
        case .unknown: nil
        }
    }
}
