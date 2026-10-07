import KeryxKit
import SwiftUI

// MARK: - The list of steps

/// The assistant's sidebar: the steps a first call needs, the optional ones, and the last
/// page, each with its state.
struct SetupStepList: View {
    let plan: SetupPlan
    @Binding var selection: SetupPage

    var body: some View {
        List(selection: Binding<SetupPage?> { selection } set: { if let page = $0 { selection = page } }) {
            Section("For a First Call") {
                ForEach(plan.steps.filter(\.required)) { row($0) }
            }
            let optional = plan.steps.filter { !$0.required }
            if !optional.isEmpty {
                Section("Optional") {
                    ForEach(optional) { row($0) }
                }
            }
            Section {
                Label {
                    Text(plan.ready ? "Ready" : "Finish")
                        .foregroundStyle(.primary)
                } icon: {
                    Image(systemName: plan.ready ? "checkmark.seal.fill" : "flag.checkered")
                        .foregroundStyle(plan.ready ? Color.green : Color.secondary)
                }
                .tag(SetupPage.finish)
            }
        }
        .listStyle(.sidebar)
    }

    private func row(_ step: SetupStep) -> some View {
        let waiting = step.kind == .blocked
        return Label {
            VStack(alignment: .leading, spacing: 1) {
                Text(SetupWording.title(step))
                    .foregroundStyle(waiting ? .secondary : .primary)
                if waiting, let why = SetupWording.waitsFor(step, in: plan) {
                    Text(why)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
        } icon: {
            Image(systemName: SetupWording.symbol(step.kind))
                .foregroundStyle(SetupWording.colour(step.kind))
                .accessibilityLabel(SetupWording.label(step.kind))
        }
        .tag(SetupPage.step(step.id))
    }
}

// MARK: - A step

/// One step's page: what it is, why it is not done (or that it is), the button that settles
/// it and the others beside it, the settings it asks for in place, and what Keryx checked.
struct SetupStepPage: View {
    let step: SetupStep
    let plan: SetupPlan
    let context: SetupContext
    /// Another button's work is under way.
    let busy: Bool
    /// The service is being installed.
    let installing: Bool
    let perform: (SetupAction) -> Void

    @Environment(AppModel.self) private var model
    /// What Keryx checked: open from the start when a check failed, since that is the why.
    @State private var detailsOpen: Bool

    init(step: SetupStep, plan: SetupPlan, context: SetupContext, busy: Bool, installing: Bool,
         perform: @escaping (SetupAction) -> Void) {
        self.step = step
        self.plan = plan
        self.context = context
        self.busy = busy
        self.installing = installing
        self.perform = perform
        _detailsOpen = State(initialValue: step.kind == .failed)
    }

    var body: some View {
        let actions = SetupGuide.actions(step, in: plan, context: context)
        VStack(spacing: 0) {
            VStack(spacing: 8) {
                Image(systemName: SetupWording.pageSymbol(step.id))
                    .font(.system(size: 38, weight: .light))
                    .foregroundStyle(.tint)
                    .frame(height: 46)
                    .accessibilityHidden(true)
                Text(SetupGuide.position(of: step, in: plan))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Text(SetupWording.title(step))
                    .font(.title.weight(.semibold))
                if let about = SetupWording.about(step.id, assistant: Wording.assistant(model.status.value)) {
                    Text(about)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: 460)
                }
                status
                    .font(.callout)
                    .padding(.top, 4)
                buttons(actions)
                    .padding(.top, 10)
            }
            .multilineTextAlignment(.center)
            .padding(.horizontal, 40)
            .padding(.top, 28)
            .padding(.bottom, 4)
            .frame(maxWidth: .infinity)

            Form {
                if step.id == "local", let display = RunDisplay.of(model.modelServerRun, finished: {
                    ("The model server is serving \($0.servedModel).", nil)
                }) {
                    Section {
                        modelServerRun(display)
                    }
                }
                settings
                if !step.checks.isEmpty {
                    Section {
                        DisclosureGroup(isExpanded: $detailsOpen) {
                            ForEach(Array(step.checks.enumerated()), id: \.offset) { _, check in
                                CheckLine(title: check.title, state: check.state, detail: check.detail,
                                          titles: Wording.titles(context.config),
                                          action: checkAction(check, besides: actions),
                                          disabled: actions.waiting, perform: perform)
                            }
                        } label: {
                            Text(step.checks.count == 1 ? "What Keryx Checked" : "What Keryx Checked (\(step.checks.count))")
                        }
                    }
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
        }
    }

    /// Done; or why not, as a plain sentence (`SetupWording.headline`).
    @ViewBuilder private var status: some View {
        let symbol = SetupWording.statusSymbol(step.kind)
        if step.id == "pin", context.pin?.place == .environment {
            Label(SetupWording.pinFromEnvironment, systemImage: "info.circle")
        } else if let line = SetupWording.headline(step, in: plan, context: context) {
            Label {
                Text(line)
            } icon: {
                Image(systemName: symbol.name)
                    .foregroundStyle(symbol.colour)
                    .accessibilityLabel(SetupWording.label(step.kind))
            }
        }
    }

    @ViewBuilder private func buttons(_ actions: SetupActions) -> some View {
        if actions.primary != nil || !actions.others.isEmpty {
            HStack(spacing: 10) {
                if let primary = actions.primary {
                    // Prominent while it is what is left to do; once done, one more button.
                    if step.kind == .done {
                        Button(primary.title) { perform(primary) }
                            .disabled(disabled(primary))
                    } else {
                        Button(primary.title) { perform(primary) }
                            .buttonStyle(.borderedProminent)
                            .disabled(disabled(primary))
                    }
                }
                ForEach(actions.others) { action in
                    // A step that waits shows its own buttons, not yet to be pressed.
                    Button(action.title) { perform(action) }
                        .disabled(actions.waiting || disabled(action))
                }
                if installing {
                    ProgressView().controlSize(.small)
                }
            }
            .controlSize(.large)
        }
    }

    /// A check's own fix, as a button beside it — unless the step's buttons offer it already.
    private func checkAction(_ check: SetupCheck, besides actions: SetupActions) -> SetupAction? {
        guard check.state != "ok", let action = FixAction(check.fix)?.action(context, named: check.title,
                                                                                 missing: check.state == "missing") else {
            return nil
        }
        if case let .setting(key)? = FixAction(check.fix), SetupGuide.settings(step).contains(key) { return nil }
        let offered = [actions.primary].compactMap { $0 } + actions.others
        return offered.contains { $0.effect == action.effect } ? nil : action
    }

    /// `keryx models start` from this page: its steps, Cancel; then that it serves, or why not.
    @ViewBuilder private func modelServerRun(_ display: RunDisplay) -> some View {
        let run = model.modelServerRun
        if case let .failed(failure) = display, LocalRefusal.of(failure.message) == .needsModel {
            RunGuidanceRow(title: ModelsWording.chooseModelFirst, message: ModelsWording.chooseModelDetail) {
                Button("Dismiss") { run.dismiss() }
            }
        } else {
            StreamRunView(groups: [RunStepGroup(steps: RunSteps.steps(RunSteps.modelServer, reached: run.lastStep,
                                                                      stage: RunSteps.stage(run.phase)))],
                          display: display, cancel: { model.cancelModelServer() },
                          failureTitle: ModelsWording.serverFailed, paths: model.status.value?.paths) {
                Button("Dismiss") { run.dismiss() }
                Button("Try Again") { model.runModelServer() }
            }
        }
    }

    private func disabled(_ action: SetupAction) -> Bool {
        switch action.effect {
        case .installService: busy
        case .startModelServer: !model.canStartModelServer
        case .downloadVoiceModels: !model.canDownloadVoiceModels
        case .mark: busy
        default: false
        }
    }

    /// The questions the step asks in place (the owner's name and numbers, the default
    /// agent), as Configuration edits them.
    @ViewBuilder private var settings: some View {
        let keys = SetupGuide.settings(step)
        if !keys.isEmpty {
            Section {
                if let rows = context.config {
                    ForEach(keys.compactMap { key in rows.first { $0.key == key } }) { row in
                        ConfigRowView(row: row, titles: Wording.titles(rows))
                    }
                } else {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Reading the settings…").foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}

// MARK: - The last page

/// "You're Ready" once the plan is, with the optional steps still open; until then, the
/// required steps left, each a click away.
struct SetupFinishPage: View {
    let plan: SetupPlan
    let context: SetupContext
    let go: (SetupPage) -> Void
    /// Runs a step's own button from its row.
    var perform: (SetupAction) -> Void = { _ in }

    @Environment(AppModel.self) private var model

    var body: some View {
        let list = SetupWording.finishList(plan)
        VStack(spacing: 0) {
            VStack(spacing: 10) {
                Image(systemName: plan.ready ? "checkmark.seal.fill" : "list.bullet.clipboard")
                    .font(.system(size: 46, weight: .light))
                    .foregroundStyle(plan.ready ? Color.green : Color.orange)
                    .frame(height: 56)
                    .accessibilityHidden(true)
                Text(SetupWording.finishTitle(plan))
                    .font(.title.weight(.semibold))
                Text(SetupWording.finishMessage(plan, assistant: Wording.assistant(model.status.value),
                                                number: context.twilioNumber))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .multilineTextAlignment(.center)
            .padding(.horizontal, 40)
            .padding(.top, 40)
            .padding(.bottom, 4)
            .frame(maxWidth: .infinity)

            Form {
                if !list.isEmpty {
                    Section(SetupWording.finishListTitle(plan)) {
                        ForEach(list) { step in
                            LabeledContent {
                                // The verb that settles it ("Add OpenAI API Key…", "Sign In to
                                // Claude…"); a step that waits, or has none, is shown instead.
                                let action = SetupGuide.actions(step, in: plan, context: context)
                                if step.kind != .blocked, let primary = action.primary {
                                    Button(SetupWording.rowTitle(primary)) { perform(primary) }
                                        .help(primary.title)
                                } else {
                                    Button("Show Step") { go(.step(step.id)) }
                                }
                            } label: {
                                Label {
                                    Text(SetupWording.title(step))
                                    if let line = SetupWording.headline(step, in: plan, context: context) {
                                        Text(line)
                                    }
                                } icon: {
                                    Image(systemName: SetupWording.symbol(step.kind))
                                        .foregroundStyle(SetupWording.colour(step.kind))
                                        .accessibilityLabel(SetupWording.label(step.kind))
                                }
                            }
                        }
                    }
                }
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
        }
    }
}
