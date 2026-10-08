import AppKit
import KeryxKit
import SwiftUI

/// The setup wizard: one frame at a time in the window with id `KeryxWindowID.setup`, from
/// Welcome to You're Ready. Opened by itself once at launch while Keryx is not ready, and
/// from "Run Setup Wizard…" (review) or the Overview's Setup Checklist (first run).
///
/// It renders `keryx setup plan` and the settings: each frame writes its answers on Continue,
/// the plan is read again, and the next frame that has something to ask is shown. Quitting
/// loses nothing; opening it again starts at Welcome and lands on what is left.
public struct SetupWizardView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismissWindow) private var dismissWindow
    @Environment(\.openURL) private var openURL
    @State private var wizard: SetupWizard
    /// The catalog's own wizard, at a frame: never restarted by the model's opening.
    private let fixed: Bool
    /// Reads Keryx as it appears (the catalog's loading state reads nothing).
    private let reads: Bool

    public init() {
        _wizard = State(initialValue: SetupWizard())
        fixed = false
        reads = true
    }

    /// The catalog's: a run already at `path`'s last frame.
    package init(wizard: SetupWizard, reads: Bool = true) {
        wizard.recordsCompletion = false
        _wizard = State(initialValue: wizard)
        fixed = true
        self.reads = reads
    }

    public var body: some View {
        content
            .frame(width: WizardMetrics.size.width, height: WizardMetrics.size.height)
            .environment(wizard)
            .task(id: model.wizardOpening.id) {
                if !fixed { wizard.start(model.wizardOpening) }
                if reads { await refresh() }
            }
            .onWindowBecomingKey { if reads { Task { await refresh() } } }
            // Opened before Keryx was found: read everything once it is.
            .onChange(of: model.client == nil) { _, missing in
                if !missing, reads { Task { await refresh() } }
            }
            .closeDisabled(model.migrationRun.isRunning)
            .remembersStops(wizard)
            .onDisappear {
                guard !fixed else { return }
                // The agent's test is the wizard's: it goes with the window.
                model.smokeRun.cancel()
            }
    }

    @ViewBuilder private var content: some View {
        if model.client == nil, model.location.error != nil {
            notFound
        } else if model.setupPlan.value == nil {
            waiting
        } else {
            frame
                .id(wizard.current)
                .transition(.opacity)
                .animation(.easeInOut(duration: 0.18), value: wizard.current)
        }
    }

    @ViewBuilder private var frame: some View {
        switch wizard.current {
        case .welcome: WelcomeFrame(close: close)
        case .migrate: MoveFilesFrame(close: close)
        case .aboutYou: AboutYouFrame()
        case .models: ModelsFrame()
        case .phone: PhoneLineFrame()
        case .finish: FinishFrame(close: close, showMain: showMainWindow)
        }
    }

    /// The plan is being read, or could not be: Welcome's frame, saying so — a Keryx older
    /// than the app said as needing an update, since trying again cannot help.
    @ViewBuilder private var waiting: some View {
        if let error = model.setupPlan.error, error.isOlderKeryx {
            WizardPage(symbol: .mark, title: WizardWording.needsUpdatingTitle, sentence: WizardWording.needsUpdating,
                       position: nil, hero: true) {
                EmptyView()
            } trailing: {
                Button("Not Now") { close() }
                    .keyboardShortcut(.cancelAction)
                Button("How to Update…") { openURL(LocateView.installURL) }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
        } else if let error = model.setupPlan.error, !model.setupPlan.isLoading {
            WizardPage(symbol: .mark, title: WizardWording.welcomeTitle, sentence: error.message, position: nil,
                       hero: true) {
                if let details = error.details {
                    DetailsDisclosure(details).frame(maxWidth: 420)
                }
            } trailing: {
                Button("Not Now") { close() }
                    .keyboardShortcut(.cancelAction)
                Button("Try Again") { Task { await model.refresh(.setupPlan) } }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
            }
        } else {
            WizardPage(symbol: .mark, title: WizardWording.welcomeTitle, sentence: WizardWording.checkingThisMac,
                       position: nil, hero: true) {
                ProgressView().controlSize(.regular)
            } trailing: {
                Button("Not Now") { close() }
                    .keyboardShortcut(.cancelAction)
                Button("Get Started") {}
                    .buttonStyle(.borderedProminent)
                    .disabled(true)
            }
        }
    }

    private var notFound: some View {
        WizardPage(symbol: .system("questionmark.folder", .secondary), title: WizardWording.notFoundTitle,
                   sentence: WizardWording.notFound, position: nil, hero: true) {
            EmptyView()
        } trailing: {
            Button("Not Now") { close() }
                .keyboardShortcut(.cancelAction)
            Button("Locate Keryx…") { showMainWindow() }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
        }
    }

    private func close() {
        dismissWindow(id: KeryxWindowID.setup)
    }

    private func showMainWindow() {
        openWindow(id: KeryxWindowID.main)
        NSApp.activate()
    }

    /// What the frames are views of: the plan, the settings, the voice, the models, the PIN,
    /// the units, the status (the assistant's name) and the doctor (the sign-ins).
    private func refresh() async {
        await model.refresh(.setupPlan, .config, .voice, .models, .pin, .service, .status, .doctor)
    }
}

extension View {
    /// Remembers in `wizard` what the owner stopped, from wherever they stopped it (a row
    /// here, or Local Models): a run that was cancelling and has gone. Nothing restarts it
    /// by itself.
    func remembersStops(_ wizard: SetupWizard) -> some View {
        modifier(StopWatcher(wizard: wizard))
    }

    /// The window's close button is off while `disabled`: a migration under way finishes
    /// before its window can go.
    func closeDisabled(_ disabled: Bool) -> some View {
        background(CloseButtonSwitch(disabled: disabled))
    }
}

private struct CloseButtonSwitch: NSViewRepresentable {
    let disabled: Bool

    func makeNSView(context: Context) -> SwitchView { SwitchView() }

    func updateNSView(_ view: SwitchView, context: Context) {
        view.disabled = disabled
    }

    /// Sets its window's close button whenever it is told, and once it is in a window.
    final class SwitchView: NSView {
        var disabled = false {
            didSet { apply() }
        }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            apply()
        }

        private func apply() {
            window?.standardWindowButton(.closeButton)?.isEnabled = !disabled
        }
    }
}

/// What counts as the owner stopping a run, wherever they stopped it: it was cancelling,
/// and now it has gone. A run that failed or finished was not stopped.
package enum StopWatch {
    /// The downloads that were cancelling and are gone.
    package static func downloads(old: [String: Downloads.Phase], new: [String: Downloads.Phase]) -> Set<LocalStep> {
        Set(old.compactMap { name, phase in phase == .cancelling && new[name] == nil ? .download(name) : nil })
    }

    package static func stopped(was cancelling: Bool, now gone: Bool) -> Bool { cancelling && gone }

    /// A model server stopped while it was the voice's first part is the voice's setup.
    package static func server(chained: Bool) -> LocalStep { chained ? .setUpVoice : .startServer }
}

private struct StopWatcher: ViewModifier {
    let wizard: SetupWizard
    @Environment(AppModel.self) private var model

    func body(content: Content) -> some View {
        content
            .onChange(of: model.downloads.active) { old, new in
                wizard.stopped.formUnion(StopWatch.downloads(old: old, new: new))
            }
            .onChange(of: model.voiceDownloadRun.phase) { old, new in
                if StopWatch.stopped(was: old == .cancelling, now: new == nil) { wizard.stopped.insert(.downloadSpeech) }
            }
            .onChange(of: model.voiceSetup.phase) { old, new in
                if StopWatch.stopped(was: old == .cancelling, now: new == nil) { wizard.stopped.insert(.setUpVoice) }
            }
            .onChange(of: model.modelServerRun.phase) { old, new in
                guard StopWatch.stopped(was: old == .cancelling, now: new == nil) else { return }
                wizard.stopped.insert(StopWatch.server(chained: model.thisMacSetup?.startsServer == true))
            }
    }
}
