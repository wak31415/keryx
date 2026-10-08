import AppKit
import KeryxKit
import SwiftUI

/// Whether the app opens at login, and the way to change it: the app's `SMAppService`
/// registration, handed in so this screen can be drawn without one.
public struct LoginItemControl {
    public enum State: Equatable, Sendable {
        case on, off
        /// Registered, but the owner has yet to allow it in System Settings.
        case needsApproval
    }

    public var state: () -> State
    public var set: (Bool) throws -> Void
    public var openSystemSettings: () -> Void
    /// Whether the registration survives a rebuild: this copy is signed with the local
    /// identity, so every build has the same designated requirement. Signed ad hoc, it does
    /// not.
    public var survivesRebuilds: Bool

    public init(state: @escaping () -> State, set: @escaping (Bool) throws -> Void,
                openSystemSettings: @escaping () -> Void, survivesRebuilds: Bool = true) {
        self.state = state
        self.set = set
        self.openSystemSettings = openSystemSettings
        self.survivesRebuilds = survivesRebuilds
    }
}

/// The app's own settings (Keryx's settings are its Configuration screen): which Keryx it
/// talks to, and whether it opens at login.
public struct AppSettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.preferences) private var preferences
    private let loginItem: LoginItemControl
    /// `Preferences` is not observable, so the chosen folder is mirrored here.
    @State private var folder: String?
    @State private var login: LoginItemControl.State = .off
    @State private var loginError: String?

    public init(loginItem: LoginItemControl) {
        self.loginItem = loginItem
    }

    public var body: some View {
        Form {
            Section("Keryx") {
                Picker("Find Keryx", selection: folderChoice) {
                    Text("Automatically").tag(FolderChoice.automatic)
                    if let folder {
                        Text(Self.tilde(folder)).tag(FolderChoice.chosen)
                    }
                    Divider()
                    Text("Choose Folder…").tag(FolderChoice.choose)
                }
                LabeledContent("Location") { location }
            }
            Section {
                Toggle("Open at Login", isOn: loginBinding)
                if login == .needsApproval {
                    LabeledContent {
                        Button("Open Login Items…") { loginItem.openSystemSettings() }
                    } label: {
                        Text("Waiting for your approval")
                        Text("Allow Keryx in System Settings to open it at login.")
                    }
                }
                if let loginError {
                    Text(loginError)
                        .problemStyle()
                        .textSelection(.enabled)
                }
            } header: {
                Text("General")
            } footer: {
                Text(Self.loginFooter(survivesRebuilds: loginItem.survivesRebuilds))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .formStyle(.grouped)
        .frame(width: 460)
        .fixedSize(horizontal: false, vertical: true)
        .onAppear {
            folder = preferences.keryxFolder
            login = loginItem.state()
        }
    }

    // MARK: - Location

    @ViewBuilder private var location: some View {
        if model.location.isLoading {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Looking…").foregroundStyle(.secondary)
            }
        } else if let found = model.location.value {
            VStack(alignment: .trailing, spacing: 2) {
                Text(Self.tilde(found.checkout ?? found.executable))
                    .textSelection(.enabled)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(Self.describe(found.source))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if folder != nil, found.source != .preference {
                    Text("The folder you chose isn't a Keryx checkout.")
                        .font(.caption)
                        .problemStyle(.warning)
                }
            }
        } else if model.location.error != nil {
            Text("Not found").foregroundStyle(.secondary)
        } else {
            Text("Not looked for yet").foregroundStyle(.secondary)
        }
    }

    static func describe(_ source: KeryxLocation.Source) -> String {
        switch source {
        case .launchAgent: "Found where Keryx runs from"
        case .preference: "Chosen by you"
        case .loginShell: "Found on your PATH"
        }
    }

    /// What Open at Login needs, under it. Signed ad hoc, a rebuild changes the signature the
    /// login item names, so it must be turned off and on again; with the local identity, not.
    static func loginFooter(survivesRebuilds: Bool) -> String {
        let opens = "Keryx must be in Applications. At login it opens in the menu bar and the Dock, "
            + "with no window, and starts answering calls."
        guard !survivesRebuilds else { return opens }
        return opens + " This copy is signed ad hoc, so after each rebuild turn this off and on again, "
            + "until builds are signed with a local signing identity."
    }

    static func tilde(_ path: String) -> String {
        (path as NSString).abbreviatingWithTildeInPath
    }

    // MARK: - Choosing a folder

    private enum FolderChoice: Hashable { case automatic, chosen, choose }

    private var folderChoice: Binding<FolderChoice> {
        Binding {
            folder == nil ? .automatic : .chosen
        } set: { choice in
            switch choice {
            case .automatic: use(nil)
            case .chosen: break
            case .choose: if let path = chooseFolder() { use(path) }
            }
        }
    }

    private func chooseFolder() -> String? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose the folder Keryx was cloned into."
        panel.prompt = "Choose"
        if let folder { panel.directoryURL = URL(fileURLWithPath: folder) }
        return panel.runModal() == .OK ? panel.url?.path : nil
    }

    private func use(_ path: String?) {
        guard path != folder else { return }
        folder = path
        preferences.keryxFolder = path
        Task { await model.relocate() }
    }

    // MARK: - Login

    private var loginBinding: Binding<Bool> {
        Binding {
            login != .off
        } set: { on in
            do {
                try loginItem.set(on)
                loginError = nil
            } catch {
                loginError = error.localizedDescription
            }
            login = loginItem.state()
        }
    }
}
