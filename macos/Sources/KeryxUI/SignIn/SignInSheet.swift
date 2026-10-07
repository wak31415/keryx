import AppKit
import KeryxKit
import SwiftUI
import UniformTypeIdentifiers

/// Signs in to Claude, Codex, Gmail or Google for agents without a terminal: `keryx auth
/// login NAME --json`, run in `AppModel.signInRun`.
///
/// The page the CLI hands over is opened in the browser once, when it arrives (an https
/// page only). Claude's code appears to be pasted only after the CLI asks for it, and
/// Gmail's landing address once the page is open; either goes to the CLI's stdin as one
/// line and is cleared from the field as it goes. Codex's code is shown to type in the
/// browser. Google's two sign-ins ask for the downloaded OAuth client file first when none
/// is saved (`--client-file`).
///
/// One sign-in runs at a time whichever it is; Cancel stops it, and closing the sheet any
/// other way stops it too, since nothing else could finish it.
public struct SignInSheet: View {
    let kind: SignInProvider

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @State private var startedHere: Bool
    @State private var sent: Bool
    @State private var typed = ""
    @State private var clientFile: String?
    @State private var opened: String?

    public init(kind: SignInProvider) {
        self.init(kind: kind, startedHere: false)
    }

    /// The catalog's: a sheet already attached to the run its model holds (`startedHere`),
    /// with its line already sent, or a client file already chosen.
    package init(kind: SignInProvider, startedHere: Bool, sent: Bool = false, clientFile: String? = nil) {
        self.kind = kind
        _startedHere = State(initialValue: startedHere)
        _sent = State(initialValue: sent)
        _clientFile = State(initialValue: clientFile)
    }

    private var stage: SignInStage {
        SignInStage.of(kind, phase: model.signInRun.phase, signingIn: model.signingIn,
                       startedHere: startedHere, sent: sent)
    }

    private var isGoogle: Bool { kind == .gmail || kind == .googleWorkspace }

    public var body: some View {
        let stage = stage
        VStack(alignment: .leading, spacing: 0) {
            header
            Form {
                content(stage)
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            .scrollDisabled(true)
            .fixedSize(horizontal: false, vertical: true)
            buttons(stage)
        }
        .frame(width: 480)
        .onChange(of: stage.link?.url) { _, url in
            guard let url, url != opened else { return }
            opened = url
            if let page = SignInWording.openable(stage.link) { openURL(page) }
        }
        .onDisappear {
            typed = ""
            if self.stage.isRunning { model.signInRun.cancel() }
        }
    }

    // MARK: - Parts

    private var header: some View {
        HStack(spacing: 12) {
            IconTile(symbol: Self.symbol(kind), tint: Self.tint(kind))
            Text(SignInWording.title(kind))
                .font(.headline)
        }
        .padding([.horizontal, .top], 20)
    }

    @ViewBuilder private func content(_ stage: SignInStage) -> some View {
        switch stage {
        case .ready:
            Section {
                Text(SignInWording.intro(kind))
                    .fixedSize(horizontal: false, vertical: true)
                if isGoogle { clientFileRow(required: GoogleClient.needsFile(model.integrations.value)) }
            }
        case let .busyElsewhere(other):
            Section {
                Label {
                    Text(SignInWording.busyElsewhere(other))
                } icon: {
                    Image(systemName: "hourglass").foregroundStyle(.secondary)
                }
            }
        case .starting, .browser, .checking, .cancelling:
            Section {
                waitingRow(stage)
                if let link = stage.link { openAgainRow(link) }
            }
        case let .deviceCode(link):
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    Text(SignInWording.deviceCodeLead)
                    HStack(spacing: 12) {
                        Text(link.code ?? "")
                            .font(.system(.title, design: .monospaced).weight(.semibold))
                            .textSelection(.enabled)
                            .accessibilityLabel("Code \(link.code ?? "")")
                        Spacer()
                        Button("Copy Code") { Self.copy(link.code ?? "") }
                            .disabled(link.code == nil)
                    }
                }
                .padding(.vertical, 2)
                waitingRow(stage)
                openAgainRow(link)
            }
        case let .wantsCode(link):
            Section {
                TextField("Code", text: $typed, prompt: Text(SignInWording.codePrompt))
                    .onSubmit(send)
                openAgainRow(link)
            } footer: {
                Text("Goes straight to Claude's sign-in, and is never kept.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        case let .wantsAddress(link):
            Section {
                TextField("Address", text: $typed, prompt: Text(SignInWording.addressPrompt))
                    .onSubmit(send)
                openAgainRow(link)
            } footer: {
                Text("After you approve, Google sends your browser to an address beginning http://localhost that "
                    + "doesn't load. That is expected: copy the whole address from the address bar.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case let .failed(message):
            Section {
                Label {
                    Text(SignInWording.failure(message))
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                if isGoogle, GoogleClient.isMissing(message) || clientFile != nil {
                    clientFileRow(required: true)
                }
            }
        case let .finished(done):
            Section {
                Label {
                    Text(done.message)
                    if let more = SignInWording.finished(done) { Text(more) }
                } icon: {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                }
            }
        }
    }

    private func waitingRow(_ stage: SignInStage) -> some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(SignInWording.waiting(stage, provider: kind) ?? StreamRun<SignedIn>.starting)
                .foregroundStyle(.secondary)
        }
    }

    /// The page again, for a browser that did not open it (or a tab closed too soon).
    private func openAgainRow(_ link: StreamRun<SignedIn>.Link) -> some View {
        LabeledContent {
            HStack(spacing: 8) {
                Button("Copy Link") { Self.copy(link.url) }
                Button("Open Again") {
                    if let page = SignInWording.openable(link) { openURL(page) }
                }
                .disabled(SignInWording.openable(link) == nil)
            }
        } label: {
            Text("Browser didn't open?")
                .foregroundStyle(.secondary)
        }
    }

    /// Google's OAuth client: the file chosen, or the button that chooses it.
    private func clientFileRow(required: Bool) -> some View {
        LabeledContent {
            Button(clientFile == nil ? "Choose File…" : "Change…") { chooseClientFile() }
        } label: {
            Text("OAuth client file")
            if let clientFile {
                Text((clientFile as NSString).lastPathComponent)
            } else if required {
                Text("Needed once: a Desktop app client made in Google Cloud, downloaded as JSON")
            } else if model.integrations.value == nil {
                Text("Needed once; choose it if Google's sign-in asks for it")
            } else {
                Text("Saved; choose another only to replace it")
            }
        }
    }

    @ViewBuilder private func buttons(_ stage: SignInStage) -> some View {
        HStack(spacing: 8) {
            if case .ready = stage, isGoogle, GoogleClient.needsFile(model.integrations.value) {
                Link("How to Make One", destination: GoogleClient.console)
                    .font(.callout)
            }
            Spacer()
            switch stage {
            case .ready:
                Button("Cancel", role: .cancel) { close() }
                    .keyboardShortcut(.cancelAction)
                Button("Sign In", action: start)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canStart)
            case let .busyElsewhere(other):
                Button(other.map { "Stop \(SignInWording.name($0)) Sign-In" } ?? "Stop Other Sign-In") {
                    model.signInRun.cancel()
                }
                .disabled(!model.signInRun.isRunning)
                Button("Close", role: .cancel) { close() }
                    .keyboardShortcut(.cancelAction)
            case .wantsCode, .wantsAddress:
                Button("Cancel", role: .cancel) { model.signInRun.cancel() }
                    .keyboardShortcut(.cancelAction)
                Button("Continue", action: send)
                    .keyboardShortcut(.defaultAction)
                    .disabled(SignInWording.line(typed).isEmpty)
            case .starting, .browser, .deviceCode, .checking:
                Button("Cancel", role: .cancel) { model.signInRun.cancel() }
                    .keyboardShortcut(.cancelAction)
            case .cancelling:
                Button("Cancel", role: .cancel) {}
                    .disabled(true)
            case let .failed(message):
                Button("Close", role: .cancel) { close() }
                    .keyboardShortcut(.cancelAction)
                Button("Try Again", action: start)
                    .keyboardShortcut(.defaultAction)
                    .disabled(isGoogle && GoogleClient.isMissing(message) && clientFile == nil)
            case .finished:
                Button("Done") { close() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding([.horizontal, .bottom], 20)
    }

    // MARK: - Actions

    private var canStart: Bool {
        guard !model.signInRun.isBusy else { return false }
        return !(isGoogle && GoogleClient.needsFile(model.integrations.value) && clientFile == nil)
    }

    private func start() {
        sent = false
        typed = ""
        opened = nil
        model.signInRun.dismiss()
        if model.signIn(kind, clientFile: isGoogle ? clientFile : nil) { startedHere = true }
    }

    /// The code or address, as one line, to the CLI's stdin; the field is emptied whether
    /// or not it went.
    private func send() {
        let line = SignInWording.line(typed)
        typed = ""
        guard !line.isEmpty else { return }
        if model.signInRun.send(line) { sent = true }
    }

    private func close() {
        typed = ""
        if startedHere, !model.signInRun.isBusy { model.signInRun.dismiss() }
        dismiss()
    }

    private func chooseClientFile() {
        let panel = NSOpenPanel()
        panel.title = "Choose the Google OAuth Client File"
        panel.message = "The JSON you downloaded for a Desktop app client in Google Cloud."
        panel.prompt = "Choose"
        panel.allowedContentTypes = [.json]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        guard panel.runModal() == .OK, let url = panel.url else { return }
        clientFile = url.path
    }

    private static func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    static func symbol(_ provider: SignInProvider) -> String {
        switch provider {
        case .claude: "sparkle"
        case .codex: "chevron.left.forwardslash.chevron.right"
        case .gmail: IntegrationsWording.symbol("gmail")
        case .googleWorkspace: IntegrationsWording.symbol("google")
        }
    }

    static func tint(_ provider: SignInProvider) -> Color {
        switch provider {
        case .claude: .orange
        case .codex: .indigo
        case .gmail: IntegrationsWording.tint("gmail")
        case .googleWorkspace: IntegrationsWording.tint("google")
        }
    }
}
