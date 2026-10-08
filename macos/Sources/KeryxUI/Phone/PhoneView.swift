import KeryxKit
import SwiftUI

/// The Phone section: the Twilio account, the number Keryx answers, where that number's
/// calls go (and pointing them here, the one write Keryx makes to Twilio, after a
/// question), the tunnel in front of this Mac, and who may call.
///
/// The account's numbers and the webhooks ask Twilio, so they are read when this section
/// shows (and by Refresh while it does), never by a refresh elsewhere.
public struct PhoneView: View {
    @Environment(AppModel.self) private var model

    public init() {}

    /// What this screen does for `request` (`RequestAnswer`); `nil` for another's.
    static func answers(_ request: AppRequest) -> PhoneRequest? {
        if case let .phone(what) = RequestAnswer.of(request) { what } else { nil }
    }

    public var body: some View {
        StateView(model.config, section: "Phone", retry: { Task { await model.refresh(.config) } }) { rows in
            PhoneForm(rows: rows)
        }
        .task {
            if model.config.value == nil { await model.refresh(.config) }
        }
        .task {
            await model.refreshPhone()
        }
    }
}

private struct PhoneForm: View {
    let rows: [ConfigRow]

    @Environment(AppModel.self) private var model
    @Environment(\.openURL) private var openURL
    @State private var editingAccount = false
    @State private var confirmingApply = false

    var body: some View {
        let titles = Wording.titles(rows)
        Form {
            if model.configNeedsRestart {
                RestartBanner()
            }
            account(titles: titles)
            number(titles: titles)
            webhooks(titles: titles)
            tunnel(titles: titles)
            Section("Callers") {
                ForEach(PhoneWording.callerKeys.compactMap(row), id: \.key) {
                    ConfigRowView(row: $0, titles: titles)
                }
            }
        }
        .formStyle(.grouped)
        .sheet(isPresented: $editingAccount) {
            TwilioAccountSheet(savedSID: value("TWILIO_ACCOUNT_SID"))
        }
        .sheet(isPresented: $confirmingApply) {
            if let report = model.phoneWebhooks.value {
                PointHereSheet(report: report)
            }
        }
        // What the phone documents are read from changed: read them again.
        .onChange(of: phoneSettings) {
            Task { await model.refreshPhone() }
        }
        // The Setup Assistant's or the Overview's Connect Twilio… and Point Number Here….
        .onChange(of: model.requested, initial: true) { answerRequest() }
        .onChange(of: model.phoneWebhooks.value) { answerRequest() }
    }

    /// Does what another window asked of Phone: the account sheet; or, once Twilio has said
    /// where the number points, the Point Here question (the section alone while it can't
    /// be asked).
    private func answerRequest() {
        guard let request = model.requested, let answer = PhoneView.answers(request) else { return }
        switch answer {
        case .account:
            editingAccount = true
        case .pointHere:
            if model.phoneWebhooks.isLoading, model.phoneWebhooks.value == nil { return }
            if let report = model.phoneWebhooks.value, PhoneWording.canApply(report) { confirmingApply = true }
        }
        model.didHandle(request)
    }

    private func row(_ key: String) -> ConfigRow? { rows.first { $0.key == key } }

    private func value(_ key: String) -> String? {
        row(key).map { ConfigWording.editText($0.value) }.flatMap { $0.isEmpty ? nil : $0 }
    }

    private func isSet(_ key: String) -> Bool { row(key)?.set ?? false }

    /// The settings the phone documents depend on.
    private var phoneSettings: [String] {
        ["TWILIO_ACCOUNT_SID", "TWILIO_NUMBER", "PUBLIC_HOST"].map { value($0) ?? "" }
            + [isSet("TWILIO_AUTH_TOKEN") ? "set" : ""]
    }

    // MARK: - The account

    private var connected: Bool { value("TWILIO_ACCOUNT_SID") != nil && isSet("TWILIO_AUTH_TOKEN") }

    private func account(titles: [String: String]) -> some View {
        let sidSet = value("TWILIO_ACCOUNT_SID") != nil
        let tokenSet = isSet("TWILIO_AUTH_TOKEN")
        let line = PhoneWording.account(model.phoneNumbers.value, sidSet: sidSet, tokenSet: tokenSet, titles: titles)
        return Section("Twilio Account") {
            LabeledContent {
                HStack(spacing: 10) {
                    if model.phoneNumbers.isLoading {
                        ProgressView().controlSize(.small)
                    } else {
                        DotText(line: line)
                    }
                    Button(sidSet || tokenSet ? "Change…" : "Connect…") { editingAccount = true }
                }
            } label: {
                Text("Account")
                if let hint = line.hint { Text(hint).textSelection(.enabled) }
            }
            LabeledContent("Account SID") {
                if let sid = value("TWILIO_ACCOUNT_SID") {
                    Text(sid)
                        .font(.callout.monospaced())
                        .textSelection(.enabled)
                } else {
                    Text("Not set").foregroundStyle(.secondary)
                }
            }
            LabeledContent("Auth token") {
                Text(row("TWILIO_AUTH_TOKEN").map(ConfigWording.secretCaption) ?? "Not set")
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - The number

    private func number(titles: [String: String]) -> some View {
        let saved = value("TWILIO_NUMBER")
        return Section("Number") {
            LabeledContent {
                if let numbers = model.phoneNumbers.value, !numbers.numbers.isEmpty {
                    NumberPicker(choices: PhoneWording.choices(numbers.numbers, saved: saved), saved: saved)
                } else if model.phoneNumbers.isLoading {
                    ProgressView().controlSize(.small)
                } else {
                    Text(saved ?? "Not set").foregroundStyle(.secondary)
                }
            } label: {
                Text("Twilio number")
                Text(PhoneWording.numberCaption(model.phoneNumbers, connected: connected, titles: titles))
            }
        }
    }

    // MARK: - The webhooks

    private func webhooks(titles: [String: String]) -> some View {
        Section {
            if let report = model.phoneWebhooks.value {
                let line = PhoneWording.webhooks(report, titles: titles)
                LabeledContent {
                    if PhoneWording.canApply(report) {
                        Button("Point Here…") { confirmingApply = true }
                    }
                } label: {
                    DotLine(line: line)
                }
                if report.error == nil {
                    ForEach(PhoneWording.hooks(report)) { hook in
                        HookRow(hook: hook)
                    }
                }
            } else if let error = model.phoneWebhooks.error, !model.phoneWebhooks.isLoading {
                LabeledContent {
                    Button("Try Again") { Task { await model.refresh(.phoneWebhooks) } }
                } label: {
                    Text(error.title(loading: "Webhooks"))
                    Text(error.message).textSelection(.enabled)
                }
            } else {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Asking Twilio where calls go…").foregroundStyle(.secondary)
                }
            }
        } header: {
            Text("Where Calls Go")
        } footer: {
            Text("Pointing the number here is the one change Keryx makes in your Twilio account, and it asks first.")
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - The tunnel

    private func tunnel(titles: [String: String]) -> some View {
        Section("Tunnel") {
            if let tunnel = model.phoneTunnel.value {
                let program = PhoneWording.tunnel(tunnel)
                LabeledContent {
                    HStack(spacing: 10) {
                        DotText(line: program)
                            .help(tunnel.binary ?? "")
                        // cloudflared's own download page: the app installs no tunnel itself.
                        if let page = FixAction.tunnelPage(tunnel.installUrl) {
                            Button("How to Install…") { openURL(page) }
                        }
                    }
                } label: {
                    Text("Tunnel")
                    if let hint = program.hint { Text(hint).textSelection(.enabled) }
                }
                let service = PhoneWording.tunnelService(tunnel)
                LabeledContent {
                    DotText(line: service)
                } label: {
                    Text("Runs as a service")
                    if let hint = service.hint { Text(hint) }
                }
            } else if let error = model.phoneTunnel.error, !model.phoneTunnel.isLoading {
                LabeledContent("Tunnel") {
                    Text(error.message).foregroundStyle(.secondary).textSelection(.enabled)
                }
            } else {
                LabeledContent("Tunnel") { ProgressView().controlSize(.small) }
            }
            if let host = row("PUBLIC_HOST") {
                ConfigRowView(row: host, titles: titles)
            }
            if let tunnel = model.phoneTunnel.value {
                let line = PhoneWording.reachable(tunnel)
                LabeledContent {
                    DotText(line: line)
                } label: {
                    Text("Reachable")
                    if let hint = line.hint { Text(hint).textSelection(.enabled) }
                }
            }
        }
    }
}

// MARK: - Pieces

/// The number picker, writing `TWILIO_NUMBER` (`config set`) as soon as one is chosen.
private struct NumberPicker: View {
    let choices: [PhoneWording.NumberChoice]
    let saved: String?

    @Environment(AppModel.self) private var model
    @State private var pending: String?
    @State private var error: KeryxError?

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            HStack(spacing: 8) {
                if pending != nil {
                    ProgressView().controlSize(.small)
                }
                Picker("Twilio number", selection: selection) {
                    if saved == nil {
                        Text("Choose a Number").tag("")
                    }
                    ForEach(choices) { choice in
                        Text(choice.label).tag(choice.number)
                    }
                }
                .pickerStyle(.menu)
                .labelsHidden()
                .fixedSize()
                .disabled(pending != nil)
            }
            if let error {
                ErrorText(error.said("The number wasn't saved."))
                    .font(.caption)
            }
        }
    }

    private var selection: Binding<String> {
        Binding {
            pending ?? saved ?? ""
        } set: { number in
            guard !number.isEmpty, number != saved else { return }
            pending = number
            error = nil
            Task {
                error = await model.set(key: "TWILIO_NUMBER", to: number)
                pending = nil
                await model.refresh(.setupPlan)
            }
        }
    }
}

/// A webhook: where it points now, and where it should when that differs.
private struct HookRow: View {
    let hook: PhoneWording.Hook

    var body: some View {
        rows
    }

    @ViewBuilder private var rows: some View {
        LabeledContent {
            VStack(alignment: .trailing, spacing: 2) {
                Text(hook.now ?? "Not set")
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(hook.now ?? "")
                    .textSelection(.enabled)
            }
        } label: {
            Text(hook.title)
        }
        if !hook.matches, let expected = hook.expected {
            LabeledContent {
                Text(expected)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(expected)
                    .textSelection(.enabled)
            } label: {
                Text("Expected")
                    .foregroundStyle(.secondary)
            }
            .padding(.leading, 12)
        }
    }
}

/// A status in a few words with its dot, and a line under it when there is more to say.
private struct DotLine: View {
    let line: PhoneWording.Line

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                StatusDot(line.tone)
                Text(line.text)
            }
            if let hint = line.hint {
                Text(hint)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }
}

/// A status in a few words with its dot, for a row's trailing side.
private struct DotText: View {
    let line: PhoneWording.Line

    var body: some View {
        HStack(spacing: 6) {
            StatusDot(line.tone)
            Text(line.text)
        }
    }
}
