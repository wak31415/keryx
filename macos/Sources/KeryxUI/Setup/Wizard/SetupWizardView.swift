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
    @State private var wizard: SetupWizard
    /// The catalog's own wizard, at a frame: never restarted by the model's opening.
    private let fixed: Bool

    public init() {
        _wizard = State(initialValue: SetupWizard())
        fixed = false
    }

    /// The catalog's: a run already at `path`'s last frame.
    package init(wizard: SetupWizard) {
        _wizard = State(initialValue: wizard)
        fixed = true
    }

    public var body: some View {
        content
            .frame(width: WizardMetrics.size.width, height: WizardMetrics.size.height)
            .environment(wizard)
            .task(id: model.wizardOpening.id) {
                if !fixed { wizard.start(model.wizardOpening) }
                await refresh()
            }
            .onWindowBecomingKey { Task { await refresh() } }
            // Opened before Keryx was found: read everything once it is.
            .onChange(of: model.client == nil) { _, missing in
                if !missing { Task { await refresh() } }
            }
            .closeDisabled(model.migrationRun.isRunning)
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
        case .pin: PinFrame()
        case .phone: PhoneLineFrame()
        case .finish: FinishFrame(close: close, showMain: showMainWindow)
        }
    }

    /// The plan is being read, or could not be: Welcome's frame, saying so.
    private var waiting: some View {
        WizardPage(symbol: .mark, title: WizardWording.welcomeTitle,
                   sentence: model.setupPlan.error.map { $0.message } ?? "Checking this Mac…", position: nil) {
            if model.setupPlan.error == nil {
                ProgressView().controlSize(.regular).padding(.top, 40)
            }
        } trailing: {
            Button("Not Now") { close() }
                .keyboardShortcut(.cancelAction)
            Button("Try Again") { Task { await model.refresh(.setupPlan) } }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(model.setupPlan.error == nil)
        }
    }

    private var notFound: some View {
        WizardPage(symbol: .system("questionmark.folder", .secondary), title: "Keryx Wasn't Found",
                   sentence: "Show this app where Keryx is, and setup carries on from there.", position: nil) {
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
