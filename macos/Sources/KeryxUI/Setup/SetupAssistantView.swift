import AppKit
import KeryxKit
import SwiftUI

/// The Setup Assistant: the steps to a first working call, one page at a time, in the
/// window with id `KeryxWindowID.setup`. A renderer of `keryx setup plan`: the list of steps
/// with their states, the page of the one chosen with the button that settles it, and Back,
/// Skip and Continue. What a step's button does happens here (a secret, `setup mark`, the
/// service, the model server) or in the main window, asked through `AppModel.request`.
///
/// The plan is read again whenever the window becomes key — the owner may have finished a
/// step in the main window — and after everything done here.
public struct SetupAssistantView: View {
    /// The window's size when it opens, and the least it may be resized to.
    static let size = CGSize(width: 820, height: 600)
    static let minimum = CGSize(width: 760, height: 540)

    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismissWindow) private var dismissWindow
    @AppStorage(SetupLaunch.hiddenKey) private var hiddenAtLaunch = false
    @State private var page: SetupPage?
    @State private var secret: SecretRequest?
    @State private var confirmingInstall = false
    @State private var installing = false
    @State private var marking = false
    @State private var failure: KeryxError?
    /// The plan's reload under way, and whether another was asked for meanwhile: a change
    /// saved while the plan was being read must not be answered by the reading from before it.
    @State private var planReload: Task<Void, Never>?
    @State private var reloadAgain = false

    public init() {}

    /// Opened at `page`, for the catalog.
    package init(page: SetupPage?, installing: Bool = false) {
        _page = State(initialValue: page)
        _installing = State(initialValue: installing)
    }

    public var body: some View {
        content
            .frame(minWidth: Self.minimum.width, idealWidth: Self.size.width, maxWidth: 1100,
                   minHeight: Self.minimum.height, idealHeight: Self.size.height, maxHeight: 900)
            .task { await refresh() }
            .onWindowBecomingKey { Task { await refresh() } }
            // Opened before Keryx was found: read everything once it is.
            .onChange(of: model.client == nil) { _, missing in
                if !missing { Task { await refresh() } }
            }
            .onChange(of: model.config.value) { reloadPlan() }
            .sheet(item: $secret, onDismiss: reloadPlan) { request in
                SecretSheet(key: request.key, title: request.title, replacing: request.replacing)
            }
            .confirmationDialog("Install Keryx as a Background Service?", isPresented: $confirmingInstall) {
                Button("Install") { installService() }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Keryx and its tunnel start now, start again at every login, and restart if they stop.")
            }
            .alert(failure?.title ?? "", isPresented: Binding(
                get: { failure != nil }, set: { if !$0 { failure = nil } }
            )) {
                Button("OK") {}
            } message: {
                Text(failure?.message ?? "")
            }
    }

    @ViewBuilder private var content: some View {
        if model.client == nil, model.location.error != nil {
            ContentUnavailableView {
                Label("Keryx Wasn't Found", systemImage: "questionmark.folder")
            } description: {
                Text("Open Keryx to find it. The steps to a first call follow once it's found.")
            } actions: {
                Button("Open Keryx") { showMainWindow() }
            }
        } else {
            StateView(model.setupPlan, section: "Setup", retry: reloadPlan) { plan in
                assistant(plan)
            }
        }
    }

    private func assistant(_ plan: SetupPlan) -> some View {
        let current = SetupGuide.resolve(page, in: plan)
        let context = SetupContext(model)
        return VStack(spacing: 0) {
            HStack(spacing: 0) {
                SetupStepList(plan: plan, selection: Binding { current } set: { page = $0 })
                    .frame(width: 236)
                Divider()
                switch current {
                case let .step(id):
                    if let step = plan.step(id) {
                        SetupStepPage(step: step, plan: plan, context: context, busy: busy,
                                      installing: installing, perform: perform)
                            .id(id)
                    }
                case .finish:
                    SetupFinishPage(plan: plan, context: context) { page = $0 }
                }
            }
            Divider()
            bottomBar(current, plan: plan, context: context)
        }
    }

    // MARK: - Back, Skip, Continue

    private func bottomBar(_ current: SetupPage, plan: SetupPlan, context: SetupContext) -> some View {
        let step: SetupStep? = if case let .step(id) = current { plan.step(id) } else { nil }
        let previous = SetupGuide.page(before: current, in: plan)
        let next = SetupGuide.page(after: current, in: plan)
        // Continue is the default once there is nothing to do here: the step done, or one
        // that is optional.
        let settled = step.map { $0.kind == .done || !$0.required } ?? true
        return HStack(spacing: 8) {
            Toggle("Don't show at launch", isOn: $hiddenAtLaunch)
                .toggleStyle(.checkbox)
                .foregroundStyle(.secondary)
            Spacer()
            if model.setupPlan.isLoading {
                ProgressView().controlSize(.small)
                Text("Checking…").foregroundStyle(.secondary)
                    .padding(.trailing, 4)
            }
            Button("Back") { if let previous { page = previous } }
                .disabled(previous == nil)
            if let step, let skip = SetupGuide.skip(step, context: context) {
                // Not while the model server starts: skipping `local` under it would walk
                // away from a run still going.
                Button(skip.title) { perform(skip) }
                    .disabled(busy || model.modelServerRun.isBusy)
            }
            if current == .finish {
                Button("Done") { dismissWindow(id: KeryxWindowID.setup) }
                    .keyboardShortcut(.defaultAction)
            } else {
                Button("Continue") { if let next { page = next } }
                    .keyboardShortcut(settled ? .defaultAction : nil)
                    .disabled(next == nil)
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    // MARK: - Actions

    /// Something is under way that another button must not start beside.
    private var busy: Bool { installing || marking }

    private func perform(_ action: SetupAction) {
        switch action.effect {
        case let .request(request):
            model.request(request)
            showMainWindow()
        case let .secret(key, title, replacing):
            secret = SecretRequest(key: key, title: title, replacing: replacing)
        case let .mark(id):
            mark(id)
        case .installService:
            confirmingInstall = true
        case .startModelServer:
            model.runModelServer()
        case let .go(target):
            page = target
        case .terminal:
            openSetupInTerminal()
        }
    }

    /// Skips a step (or leaves the PIN to the first call), then moves on.
    private func mark(_ id: String) {
        marking = true
        Task {
            let error = await model.markSetupStep(id)
            marking = false
            if let error {
                failure = error
            } else if let plan = model.setupPlan.value, let next = SetupGuide.page(after: .step(id), in: plan) {
                page = next
            }
        }
    }

    private func installService() {
        installing = true
        Task {
            let answer = await model.installService()
            installing = false
            if case let .failure(error) = answer { failure = error }
        }
    }

    private func openSetupInTerminal() {
        guard let location = model.location.value else { return }
        do {
            try TerminalLauncher.open(location, arguments: ["setup"])
        } catch {
            failure = (error as? KeryxError) ?? .terminal(error.localizedDescription)
        }
    }

    private func showMainWindow() {
        openWindow(id: KeryxWindowID.main)
        NSApp.activate()
    }

    // MARK: - Reading

    /// The plan and what the buttons depend on (the settings, the voice, the models, the
    /// PIN, the units).
    private func refresh() async {
        reloadPlan()
        await model.refresh(.config, .voice, .models, .pin, .service, .status)
    }

    /// Reads the plan again; asked while a reading is under way, reads it once more after.
    private func reloadPlan() {
        guard planReload == nil else {
            reloadAgain = true
            return
        }
        planReload = Task {
            repeat {
                reloadAgain = false
                await model.refresh(.setupPlan)
            } while reloadAgain
            planReload = nil
        }
    }
}

/// The secret sheet's subject.
private struct SecretRequest: Identifiable {
    let key: String
    let title: String
    let replacing: Bool

    var id: String { key }
}
