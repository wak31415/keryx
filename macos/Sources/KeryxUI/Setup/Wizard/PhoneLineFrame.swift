import KeryxKit
import SwiftUI

/// Phone Line: four sub-steps down the frame, each enabled by the one before, each folding
/// to a ticked line once done — the Twilio account (the account sheet, which checks the pair
/// before saving), the number Lyra answers (`TWILIO_NUMBER`), the hostname Twilio reaches
/// this Mac at (`PUBLIC_HOST`), and the number pointed here. Pointing it is the one write
/// outside this Mac: its own button, through the Point Here question, never Continue's.
///
/// Continue needs the first three; Set Up Later goes to Checking and ends on Nearly Ready.
package struct PhoneLineFrame: View {
    @Environment(AppModel.self) private var model
    @Environment(SetupWizard.self) private var wizard
    @Environment(\.openURL) private var openURL
    @State private var editingAccount = false
    @State private var pointing = false
    /// The number picked, until it is saved.
    @State private var picked: String?
    @State private var host: String
    @State private var savingNumber = false
    @State private var savingHost = false
    @State private var failure: KeryxError?
    /// The sub-steps the owner asked to change, though done.
    @State private var changing: Set<Int> = []
    @State private var leaving = false
    @FocusState private var hostFocused: Bool
    @State private var noDomainShown = false
    /// Homebrew's `brew`, looked for once as the frame opens rather than on every redraw.
    @State private var homebrew: String?

    package init(host: String = "") {
        _host = State(initialValue: host)
    }

    static let console = URL(string: "https://console.twilio.com/")!
    static let signUp = URL(string: "https://www.twilio.com/try-twilio")!
    static let buyNumber = URL(string: "https://console.twilio.com/us1/develop/phone-numbers/manage/search")!

    package var body: some View {
        let facts = WizardFacts(model)
        let who = WizardWording.assistant(facts, status: model.status.value)
        let steps = PhoneSteps(facts: facts, numbers: model.phoneNumbers.value, tunnel: model.phoneTunnel.value,
                               webhooks: model.phoneWebhooks.value)
        WizardPage(symbol: .system("phone"), title: WizardWording.phoneTitle,
                   sentence: WizardWording.phoneSentence(who), position: wizard.position(facts)) {
            ScrollView {
                VStack(spacing: 6) {
                    account(steps)
                    number(steps, assistant: who)
                    reaching(steps, facts: facts)
                    pointHere(steps)
                    if let failure {
                        ErrorText(failure.said("That wasn't saved."))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .scrollBounceBehavior(.basedOnSize)
        } leading: {
            Button(WizardWording.setUpLater) { setUpLater(facts) }
                .disabled(leaving)
            if !steps.canContinue(pickedNumber: picked, typedHost: typedHost) {
                WaitingCaption(text: "Finish steps 1 to 3 to continue")
            }
        } trailing: {
            if leaving { ProgressView().controlSize(.small) }
            Button("Back") { wizard.back() }
                .keyboardShortcut(.cancelAction)
            Button("Continue") { proceed(steps) }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!steps.canContinue(pickedNumber: picked, typedHost: typedHost) || leaving)
        }
        .sheet(isPresented: $editingAccount, onDismiss: { Task { await readPhone() } }) {
            TwilioAccountSheet(savedSID: facts.text("TWILIO_ACCOUNT_SID"))
        }
        .sheet(isPresented: $pointing, onDismiss: { Task { await model.refresh(.phoneWebhooks, .setupPlan) } }) {
            if let report = model.phoneWebhooks.value { PointHereSheet(report: report) }
        }
        .task {
            if host.isEmpty { host = facts.text("PUBLIC_HOST") ?? "" }
            homebrew = model.homebrew
            await readPhone()
            // SEAM: the hostname the tunnel already routes here, when the CLI says it.
            if host.isEmpty, let known = model.phoneTunnel.value?.hostname { host = known }
        }
    }

    private var typedHost: String { host.trimmingCharacters(in: .whitespaces) }

    // MARK: - 1 · Twilio account

    @ViewBuilder private func account(_ steps: PhoneSteps) -> some View {
        if steps.accountDone, !changing.contains(1) {
            FoldedSection(title: "1  Twilio Account", summary: steps.accountName ?? "Connected") { editingAccount = true }
        } else {
            WizardSection(title: "1  Twilio Account") {
                VStack(alignment: .leading, spacing: 6) {
                    if steps.sidSet, steps.tokenSet, !model.phoneNumbers.isLoading,
                       let error = model.phoneNumbers.value?.error {
                        // Refused: what to do is in the sentence; the change is beside it.
                        HStack(alignment: .top) {
                            ErrorText(StreamFailure(PhoneSteps.refused, detail: error))
                            Spacer()
                            Button("Change…") { editingAccount = true }
                                .controlSize(.regular)
                        }
                    } else {
                        if model.phoneNumbers.isLoading {
                            StatusLine(tone: .working, text: "Checking with Twilio…")
                        } else {
                            Text(WizardWording.twilioIntro)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        HStack(spacing: 12) {
                            Link("Open Twilio", destination: Self.console)
                                .foregroundStyle(.tint)
                            if !steps.sidSet {
                                Link("Sign Up Free", destination: Self.signUp)
                                    .foregroundStyle(.tint)
                            }
                            Spacer()
                            Button(steps.sidSet ? "Change…" : "Connect…") { editingAccount = true }
                                .controlSize(.regular)
                        }
                        .font(.callout)
                    }
                }
            }
        }
    }

    // MARK: - 2 · The number

    @ViewBuilder private func number(_ steps: PhoneSteps, assistant: String) -> some View {
        let title = "2  \(assistant)'s Number"
        if steps.numberDone, !changing.contains(2) {
            FoldedSection(title: title, summary: steps.savedNumber.map(PhoneDisplay.format) ?? "") { changing.insert(2) }
        } else {
            WizardSection(title: title) {
                if !steps.accountDone {
                    StatusLine(tone: .none, text: "Connect your Twilio account first.")
                } else if steps.choices.isEmpty {
                    HStack {
                        StatusLine(tone: .warning, text: "This account has no number yet.")
                        Spacer()
                        Button("Buy One at Twilio…") { openURL(Self.buyNumber) }
                            .controlSize(.regular)
                    }
                } else {
                    HStack(spacing: 8) {
                        Picker("Number", selection: numberSelection(steps)) {
                            ForEach(steps.choices) { Text($0.label).tag($0.number) }
                        }
                        .labelsHidden()
                        .fixedSize()
                        if savingNumber { ProgressView().controlSize(.small) }
                        Spacer()
                    }
                }
            }
        }
    }

    /// The number shown: the one picked, else the saved one, else the account's first.
    private func numberSelection(_ steps: PhoneSteps) -> Binding<String> {
        Binding {
            picked ?? steps.savedNumber ?? steps.choices.first?.number ?? ""
        } set: { number in
            picked = number
            save(number: number)
        }
    }

    // MARK: - 3 · Reaching this Mac

    @ViewBuilder private func reaching(_ steps: PhoneSteps, facts: WizardFacts) -> some View {
        if steps.reachDone, !changing.contains(3) {
            FoldedSection(title: "3  Reaching This Mac",
                          summary: [steps.tunnelKind.map(PhoneSteps.tunnelName), steps.savedHost]
                              .compactMap { $0 }.joined(separator: " · ")) {
                changing.insert(3)
            }
        } else {
            WizardSection(title: "3  Reaching This Mac") {
                VStack(alignment: .leading, spacing: 6) {
                    Text(WizardWording.reachIntro)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    tunnelRow()
                    HStack(spacing: 8) {
                        Text(WizardWording.addressLabel)
                        TextField(WizardWording.addressLabel, text: $host, prompt: Text("lyra.yourdomain.com"))
                            .textFieldStyle(.roundedBorder)
                            .labelsHidden()
                            .focused($hostFocused)
                            .onSubmit { saveHost() }
                        if savingHost { ProgressView().controlSize(.small) }
                    }
                    .onChange(of: hostFocused) { _, focused in if !focused { saveHost() } }
                    HStack(alignment: .firstTextBaseline) {
                        Text(WizardWording.addressCaption)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button("No Domain?") { noDomainShown = true }
                            .buttonStyle(.link)
                            .font(.caption)
                            .foregroundStyle(.tint)
                            .popover(isPresented: $noDomainShown, arrowEdge: .bottom) {
                                Text(WizardWording.noDomain)
                                    .font(.callout)
                                    .fixedSize(horizontal: false, vertical: true)
                                    .frame(width: 280)
                                    .padding(14)
                            }
                    }
                }
            }
        }
    }

    /// Whether Cloudflare Tunnel is here; if not, the way to get it — installed by the app
    /// with Homebrew, after a click, or its download page.
    @ViewBuilder private func tunnelRow() -> some View {
        let install = model.tunnelInstall
        if let tunnel = model.phoneTunnel.value {
            switch install.phase {
            case let .running(line)?:
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        StatusLine(tone: .working, text: "Installing Cloudflare Tunnel…")
                        Text(WizardWording.tunnelInstalling)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Cancel") { install.cancel() }
                        .controlSize(.regular)
                }
                if let line { DetailsDisclosure(line) }
            case .cancelling?:
                StatusLine(tone: .working, text: "Stopping…")
            case let .failed(failure)?:
                HStack(alignment: .top) {
                    ErrorText(failure)
                    Spacer()
                    Button("Try Again") { install.dismiss(); model.installTunnel() }
                        .controlSize(.regular)
                }
            case .finished?, nil:
                HStack(spacing: 8) {
                    if let kind = tunnel.kind {
                        StatusLine(tone: .ok, text: "\(PhoneSteps.tunnelName(kind)) is installed")
                    } else if model.tunnelOutsideKeryxPath {
                        StatusLine(tone: .warning, text: WizardWording.tunnelOutsidePath)
                    } else {
                        StatusLine(tone: .warning, text: WizardWording.tunnelMissing)
                    }
                    Spacer()
                    if tunnel.kind == nil, !model.tunnelOutsideKeryxPath {
                        if homebrew != nil {
                            Button("Install Cloudflare Tunnel…") { model.installTunnel() }
                                .controlSize(.regular)
                        } else if let page = FixAction.tunnelPage(tunnel.installUrl) {
                            Button("Get Cloudflare Tunnel…") { openURL(page) }
                                .controlSize(.regular)
                        }
                        Button("Check Again") { Task { await model.refresh(.phoneTunnel) } }
                            .controlSize(.regular)
                    }
                }
            }
        } else {
            StatusLine(tone: .working, text: "Looking for a tunnel…")
        }
    }

    // MARK: - 4 · Point the number here

    @ViewBuilder private func pointHere(_ steps: PhoneSteps) -> some View {
        if steps.pointedHere {
            FoldedSection(title: "4  Point the Number Here", summary: "Its calls come to this Mac")
        } else {
            WizardSection(title: "4  Point the Number Here") {
                if !(steps.numberDone && steps.reachDone) {
                    StatusLine(tone: .none, text: "Choose the number and the address for calls first.")
                } else if let report = model.phoneWebhooks.value {
                    VStack(alignment: .leading, spacing: 6) {
                        Grid(alignment: .topLeading, horizontalSpacing: 10, verticalSpacing: 4) {
                            GridRow {
                                Text("").gridColumnAlignment(.leading)
                                Text("Now").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                                Text("Will become").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                            }
                            ForEach(PhoneWording.hooks(report)) { hook in
                                GridRow {
                                    Text(hook.title).font(.callout)
                                    url(hook.now)
                                    url(hook.expected)
                                }
                            }
                        }
                        HStack(alignment: .top) {
                            Text(WizardWording.pointHereCaption)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer()
                            Button("Point It Here…") { pointing = true }
                                .controlSize(.regular)
                                .disabled(!PhoneWording.canApply(report))
                        }
                    }
                } else {
                    StatusLine(tone: .working, text: "Asking Twilio where calls go…")
                }
            }
        }
    }

    /// An address as compared: no scheme, the host whole, wrapping rather than cut.
    private func url(_ text: String?) -> some View {
        Text(text.map(PhoneSteps.shortURL) ?? "Not set")
            .font(.caption.monospaced())
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: 190, alignment: .leading)
            .help(text ?? "")
    }

    // MARK: - Writing

    private func readPhone() async {
        await model.refresh(.config)
        await model.refreshPhone()
    }

    private func save(number: String) {
        savingNumber = true
        failure = nil
        Task {
            defer { savingNumber = false }
            if let error = await model.set(key: "TWILIO_NUMBER", to: number) {
                failure = error
                return
            }
            picked = nil
            await model.refresh(.phoneWebhooks, .setupPlan)
        }
    }

    /// Saves the address once it is committed (Return, or leaving the field), when changed.
    private func saveHost() {
        let value = typedHost
        guard !value.isEmpty, !savingHost, value != WizardFacts(model).text("PUBLIC_HOST") else { return }
        savingHost = true
        failure = nil
        Task {
            defer { savingHost = false }
            if let error = await model.set(key: "PUBLIC_HOST", to: value) {
                failure = error
                return
            }
            await model.refresh(.phoneTunnel, .phoneWebhooks, .setupPlan)
        }
    }

    /// Saves what is picked or typed and not yet saved, then moves on.
    private func proceed(_ steps: PhoneSteps) {
        leaving = true
        failure = nil
        Task {
            defer { leaving = false }
            if let error = await WizardWrite.apply(steps.writes(pickedNumber: picked, typedHost: typedHost), with: model) {
                failure = error
                return
            }
            wizard.phoneLater = false
            await wizard.moveOn(model)
        }
    }

    /// Leaves the phone for later: recorded as such when the plan lets it be (SEAM:
    /// `deferrable`), and the run ends on Nearly Ready.
    private func setUpLater(_ facts: WizardFacts) {
        leaving = true
        failure = nil
        Task {
            defer { leaving = false }
            if let error = await WizardWrite.apply(PhoneSteps.later(facts), with: model) {
                failure = error
                return
            }
            wizard.phoneLater = true
            await wizard.moveOn(model)
        }
    }
}

/// Where the phone's four sub-steps stand, from the settings and Twilio's answers.
package struct PhoneSteps {
    let facts: WizardFacts
    let numbers: PhoneNumbers?
    let tunnel: PhoneTunnel?
    let webhooks: PhoneWebhooks?

    package init(facts: WizardFacts, numbers: PhoneNumbers?, tunnel: PhoneTunnel?, webhooks: PhoneWebhooks?) {
        self.facts = facts
        self.numbers = numbers
        self.tunnel = tunnel
        self.webhooks = webhooks
    }

    /// What a refused account says; Twilio's own words (and its status code) are details.
    static let refused = "Twilio refused the Account SID and Auth Token. Check them on the Twilio Console and try again."

    /// The tunnel program by the name a person knows it by.
    static func tunnelName(_ kind: String) -> String {
        kind == "cloudflared" ? "Cloudflare Tunnel" : kind
    }

    /// "demo.twilio.com/welcome/voice/": an address without its scheme.
    static func shortURL(_ url: String) -> String {
        for scheme in ["https://", "http://"] where url.hasPrefix(scheme) { return String(url.dropFirst(scheme.count)) }
        return url
    }

    var sidSet: Bool { facts.text("TWILIO_ACCOUNT_SID") != nil }
    var tokenSet: Bool { facts.row("TWILIO_AUTH_TOKEN")?.set == true }
    var accountName: String? { numbers?.account.flatMap { $0.isEmpty ? nil : $0 } }
    /// Twilio answered with the account's name.
    package var accountDone: Bool { sidSet && tokenSet && accountName != nil }

    var choices: [PhoneWording.NumberChoice] {
        PhoneWording.choices(numbers?.numbers ?? [], saved: nil)
    }
    var savedNumber: String? { facts.text("TWILIO_NUMBER") }
    /// A number is saved, and the account has it.
    package var numberDone: Bool {
        guard let saved = savedNumber else { return false }
        return numbers?.numbers.contains { $0.number == saved } ?? false
    }

    var tunnelKind: String? { tunnel?.kind }
    var savedHost: String? { facts.text("PUBLIC_HOST") }
    /// A tunnel program is installed and a hostname is saved.
    package var reachDone: Bool { tunnelKind != nil && savedHost != nil }

    /// Twilio sends the number's calls here.
    package var pointedHere: Bool { webhooks?.matches == true && webhooks?.error == nil }

    /// What Continue saves that is not saved yet: the number shown (the account's first, if
    /// none was picked) and the hostname typed.
    package func writes(pickedNumber: String?, typedHost: String) -> [WizardWrite] {
        var writes: [WizardWrite] = []
        if savedNumber == nil || (pickedNumber != nil && pickedNumber != savedNumber),
           let number = pickedNumber ?? choices.first?.number {
            writes.append(.set("TWILIO_NUMBER", number))
        }
        if !typedHost.isEmpty, typedHost != savedHost { writes.append(.set("PUBLIC_HOST", typedHost)) }
        return writes
    }

    /// Set Up Later: recorded through the CLI when it can defer the phone (SEAM:
    /// `deferrable`), so the plan says so; otherwise nothing is written, and this run alone
    /// remembers it.
    package static func later(_ facts: WizardFacts) -> [WizardWrite] {
        facts.plan?.step("phone")?.deferrable == true ? [.mark("phone")] : []
    }

    /// The first three are done, or will be once what is picked and typed is saved.
    package func canContinue(pickedNumber: String?, typedHost: String) -> Bool {
        let number = numberDone || (accountDone && (pickedNumber != nil || !choices.isEmpty))
        let host = savedHost != nil || !typedHost.isEmpty
        return accountDone && number && tunnelKind != nil && host
    }
}
