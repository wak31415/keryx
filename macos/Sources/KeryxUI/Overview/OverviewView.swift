import AppKit
import KeryxKit
import SwiftUI

/// The first section of the window: whether Keryx is running, what its setup still needs,
/// the approval calls, and where the assistant's memory is.
public struct OverviewView: View {
    @Environment(AppModel.self) private var model

    public init() {}

    public var body: some View {
        StateView(model.status, section: "Overview", retry: { Task { await model.refresh(.status) } }) { status in
            Form {
                ServiceSection(status: status)
                SetupSection()
                ApprovalCallsSection(status: status)
                MemorySection(status: status)
            }
            .formStyle(.grouped)
        }
        .task {
            // The status and the doctor are already polled; these are read here — the
            // settings for the titles doctor's details are said in.
            await model.refresh(.approvals)
            await model.refresh(.memory)
            await model.refresh(.service)
            if model.config.value == nil { await model.refresh(.config) }
            // What the checks' buttons say: a PIN to set or change, a tunnel to install, a
            // plugin's and an account's names.
            await model.refresh(.pin, .phoneTunnel, .integrations, .plugins)
        }
    }
}

// MARK: - Keryx

private struct ServiceSection: View {
    let status: StatusReport

    @Environment(AppModel.self) private var model
    @State private var confirmingRestart = false
    @State private var liveCalls: Int?
    @State private var failure: KeryxError?
    @State private var restarting = false

    var body: some View {
        Section {
            HStack(spacing: 12) {
                KeryxMark(mode: status.tasks.running > 0 ? .working : .idle)
                    .frame(width: 40, height: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(headline)
                        .font(.title3.weight(.semibold))
                    Text(byline)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
            .padding(.vertical, 4)

            LabeledContent("Answering calls") {
                let line = answering
                HStack(spacing: 10) {
                    VStack(alignment: .trailing, spacing: 2) {
                        HStack(spacing: 6) {
                            if line.busy {
                                ProgressView().controlSize(.small)
                            } else {
                                StatusDot(line.tone)
                            }
                            Text(line.text)
                        }
                        .help(line.help)
                        if let hint = line.hint {
                            Text(hint)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.trailing)
                                .textSelection(.enabled)
                        }
                        if let details = line.details {
                            DetailsDisclosure(details)
                                .frame(maxWidth: 320)
                        }
                    }
                    if line.offersStart {
                        Button("Start") { start() }
                            .disabled(model.serviceChange != nil || model.finishingPreviousStop)
                            .help("Starts Keryx answering calls")
                    }
                }
            }

            if let tunnel = model.service.value?.units.tunnel, status.service.installed || tunnel.installed {
                LabeledContent("Tunnel") {
                    HStack(spacing: 6) {
                        let line = ServiceWording.tunnel(tunnel)
                        StatusDot(line.tone)
                        Text(line.text)
                    }
                }
            }

            LabeledContent("Calls in progress") {
                if status.health.reachable {
                    Text(calls)
                } else {
                    Text("Not reachable").foregroundStyle(.secondary)
                }
            }

            if !status.tasks.ok || status.tasks.running + status.tasks.unreported > 0 {
                LabeledContent("Sessions") {
                    Text(sessions)
                }
            }

            if let running = status.version.running {
                LabeledContent("Version") {
                    if status.health.reachable {
                        VStack(alignment: .trailing, spacing: 2) {
                            Text(OverviewWording.version(running))
                                .help(running)
                            if let pending = pendingVersion {
                                Text(pending)
                                    .font(.callout)
                                    .problemStyle(.warning)
                                    .help(status.version.checkout ?? "")
                            }
                        }
                    } else {
                        // The stamp the last start wrote: it says nothing about now.
                        Text("Last ran \(OverviewWording.version(running))")
                            .foregroundStyle(.secondary)
                            .help(running)
                    }
                }
            }

            if status.restart.state == "failed" {
                LabeledContent("Last restart") {
                    VStack(alignment: .trailing, spacing: 2) {
                        HStack(spacing: 6) {
                            StatusDot(.error)
                            Text("Failed")
                        }
                        if let reason = status.restart.failure, !reason.isEmpty {
                            Text(Wording.sentence(reason))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.trailing)
                                .textSelection(.enabled)
                        }
                    }
                }
            }

            HStack {
                if restarting {
                    ProgressView().controlSize(.small)
                    Text("Restarting…").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Reveal Logs") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: status.paths.logs)])
                }
                Button("Restart…") { confirmingRestart = true }
                    .disabled(restarting || model.serviceChange != nil || !status.service.installed)
            }
        }
        .confirmationDialog("Restart Keryx?", isPresented: $confirmingRestart) {
            Button("Restart") { restart(force: false) }
        } message: {
            Text(OverviewWording.restartMessage)
        }
        .confirmationDialog(liveCallsTitle, isPresented: Binding(
            get: { liveCalls != nil }, set: { if !$0 { liveCalls = nil } }
        )) {
            Button("Restart Anyway", role: .destructive) { restart(force: true) }
        } message: {
            Text(liveCalls == 1 ? "Restarting ends it." : "Restarting ends them.")
        }
        .alert(failure?.title ?? "", isPresented: Binding(
            get: { failure != nil }, set: { if !$0 { failure = nil } }
        )) {
            Button("OK") {}
        } message: {
            Text(failure?.message ?? "")
        }
    }

    private var headline: String {
        status.health.reachable ? "Keryx is answering calls" : model.menuState.headline
    }

    /// "Lyra · Claude": the assistant, and the agent it sends work to.
    private var byline: String {
        [status.assistant.name, Wording.agent(status.agents.default)]
            .filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// Whether Keryx answers calls, in a word (`ServiceWording.answering`).
    private var answering: ServiceWording.Answering {
        ServiceWording.answering(status, step: model.answeringStep, change: model.serviceChange,
                                 failure: model.startFailure, finishingStop: model.finishingPreviousStop)
    }

    /// Start: the fallback when Keryx should be answering and isn't. The app starts it by
    /// itself, so this asks nothing first; it goes the one way the app starts Keryx
    /// (`startAnswering`), joining a start already under way.
    private func start() {
        Task {
            if case let .failure(error) = await model.startAnswering() { failure = error }
        }
    }

    private var calls: String {
        switch status.health.liveSessions ?? 0 {
        case 0: "None"
        case let count: "\(count)"
        }
    }

    private var sessions: String {
        let tasks = status.tasks
        guard tasks.ok else { return "Unknown" }
        var parts: [String] = []
        if tasks.running > 0 { parts.append("\(tasks.running) running") }
        if tasks.unreported > 0 { parts.append("\(tasks.unreported) not yet told") }
        return parts.joined(separator: " · ")
    }

    /// Only while Keryx answers: otherwise the running stamp may be from a start long ago.
    private var pendingVersion: String? {
        guard status.health.reachable, let running = status.version.running, let checkout = status.version.checkout,
              running != checkout else { return nil }
        return "Restart to load \(OverviewWording.version(checkout))"
    }

    private var liveCallsTitle: String {
        liveCalls == 1 ? "1 call is in progress." : "\(liveCalls ?? 0) calls are in progress."
    }

    private func restart(force: Bool) {
        restarting = true
        Task {
            let error = await model.restart(force: force)
            restarting = false
            if case let .liveCalls(count) = error {
                liveCalls = count
            } else {
                failure = error
            }
        }
    }
}

// MARK: - Setup

private struct SetupSection: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow
    @State private var expanded: Set<String>?
    /// A check's button, run by `FixPerformer`.
    @State private var fixing: SetupAction?

    var body: some View {
        Section("Setup Checklist") {
            if let doctor = model.doctor.value {
                if doctor.ok {
                    Label {
                        Text("Everything Keryx needs is set up")
                    } icon: {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                    }
                } else {
                    needsAttention(doctor)
                }
            } else if let error = model.doctor.error, !model.doctor.isLoading {
                LabeledContent {
                    Button("Try Again") { Task { await model.refresh(.doctor) } }
                } label: {
                    Text(error.title(loading: "Setup"))
                    Text(error.message).textSelection(.enabled)
                }
            } else {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Checking setup…").foregroundStyle(.secondary)
                }
            }
        }
        .performsFixes($fixing)
    }

    /// One of doctor's checks, with the button its `fix` names.
    private func line(_ check: DoctorCheck, context: SetupContext) -> some View {
        CheckLine(title: ChecklistWording.title(check), state: check.state, detail: ChecklistWording.detail(check),
                  hint: check.hint, titles: titles, action: FixAction(check.fix)?.action(context, named: check.displayTitle,
                                                             missing: check.state == "missing"),
                  disabled: model.serviceChange != nil) { fixing = $0 }
            // Under the disclosure's title, not under its chevron.
            .padding(.leading, 12)
    }

    @ViewBuilder private func needsAttention(_ doctor: DoctorReport) -> some View {
        let open = doctor.checks.filter { !$0.ok }
        let passed = doctor.checks.filter(\.ok)
        LabeledContent {
            Button("Set Up Keryx…") {
                SetupLaunch.open(.checklist, model: model)
                openWindow(id: KeryxWindowID.setup)
            }
        } label: {
            Label {
                Text(open.count == 1 ? "1 thing needs attention" : "\(open.count) things need attention")
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            }
        }
        let context = SetupContext(model)
        ForEach(Self.sections(of: open), id: \.self) { section in
            DisclosureGroup(isExpanded: binding(section, default: true)) {
                ForEach(open.filter { $0.section == section }) { line($0, context: context) }
            } label: {
                Text(Self.title(ofSection: section))
            }
        }
        if !passed.isEmpty {
            DisclosureGroup(isExpanded: binding("", default: false)) {
                ForEach(passed) { line($0, context: context) }
            } label: {
                Text(passed.count == 1 ? "1 check passed" : "\(passed.count) checks passed")
            }
        }
    }

    /// The settings' titles, for doctor's details.
    private var titles: [String: String] { Wording.titles(model.config.value) }

    /// Whether a disclosure is open. Failing sections start open, the passed checks closed.
    private func binding(_ key: String, default open: Bool) -> Binding<Bool> {
        Binding {
            expanded.map { $0.contains(key) } ?? open
        } set: { isOpen in
            var keys = expanded ?? Set(Self.sections(of: model.doctor.value?.checks.filter { !$0.ok } ?? []))
            if isOpen { keys.insert(key) } else { keys.remove(key) }
            expanded = keys
        }
    }

    /// Doctor's sections, in the order its checks first name them.
    static func sections(of checks: [DoctorCheck]) -> [String] {
        var seen: [String] = []
        for check in checks where !seen.contains(check.section) { seen.append(check.section) }
        return seen
    }

    /// The titles `keryx setup` gives its sections; one this build does not know is shown
    /// as doctor names it.
    static func title(ofSection section: String) -> String {
        switch section {
        case "import": "Storage"
        case "voice": "Voice"
        case "agents": "Coding Agents"
        case "local": "Local Models"
        case "settings": "Settings"
        case "owner": "Owner and PIN"
        case "phone": "Phone"
        case "google": "Google"
        case "plugins": "Plugins"
        case "issues": "Issue Reports"
        case "profile": "About You"
        case "projects": "Project Context"
        case "service": WizardWording.answeringCalls
        case "security": "Security"
        default: Wording.capitalised(section)
        }
    }
}

/// The checklist in the wizard's words, where the doctor's would be a second vocabulary for
/// the same thing: answering calls, the Twilio account, the address for calls, the tunnel, and
/// "this Mac" for "this machine".
enum ChecklistWording {
    static func title(_ check: DoctorCheck) -> String {
        switch check.name {
        case "Twilio credentials": "Twilio Account"
        case "PUBLIC_HOST": "Address for Calls"
        case "tunnel": "Cloudflare Tunnel"
        default: CheckWording.title(check.displayTitle)
        }
    }

    /// What was found, said plainly where the wizard says it; anything else as the doctor
    /// says it, "this machine" said as "this Mac". The "background process" check's own
    /// two ways to fail (`missing`: never installed; `failed`, Task N: installed, not
    /// running) are wording, never a unit's name — `CheckWording.detail` is what reads
    /// the raw detail and strips it, keeping the full original behind Show Details, so
    /// that stays true here too rather than being duplicated or short-circuited.
    static func detail(_ check: DoctorCheck) -> String {
        if !check.ok {
            switch check.name {
            case "Twilio credentials" where check.state == "missing": return "Not connected"
            case "PUBLIC_HOST" where check.state == "missing": return "Not set"
            case "tunnel" where check.detail.contains("not installed"): return "Not installed"
            default: break
            }
        }
        return check.detail.replacingOccurrences(of: "this machine", with: "this Mac")
    }
}

// MARK: - Approval calls

private struct ApprovalCallsSection: View {
    let status: StatusReport

    @Environment(AppModel.self) private var model
    @State private var pending: Bool?
    @State private var confirmingOff = false
    @State private var failure: KeryxError?
    @State private var showingAll = false

    var body: some View {
        Section("Approval Calls") {
            Toggle(isOn: enabled) {
                Text("Call me about approvals")
                if let caption { Text(caption) }
            }
            .disabled(pending != nil)
            if let failure {
                Text(failure.message)
                    .font(.caption)
                    .problemStyle()
                    .textSelection(.enabled)
            }
            if let report = model.approvals.value, !report.entries.isEmpty {
                ForEach(ApprovalWording.requests(report.entries).filter { !$0.standalone }.prefix(5)) {
                    ApprovalRow(request: $0)
                }
                HStack {
                    Spacer()
                    Button("Show All…") { showingAll = true }
                }
            }
        }
        .confirmationDialog("Turn Off Approval Calls?", isPresented: $confirmingOff) {
            Button("Turn Off") { write(false) }
        } message: {
            Text(ApprovalCallsWording.offWarning)
        }
        .sheet(isPresented: $showingAll) {
            ApprovalsSheet()
        }
    }

    private var actual: Bool {
        model.approvals.value?.escalationEnabled ?? status.approvals.escalationEnabled
    }

    private var enabled: Binding<Bool> {
        Binding {
            pending ?? actual
        } set: { on in
            if on { write(true) } else { confirmingOff = true }
        }
    }

    /// "After 5 minutes · at most 4 an hour", once the approvals have loaded.
    private var caption: String? {
        guard let report = model.approvals.value else { return nil }
        let minutes = Int((report.escalateAfterS / 60).rounded())
        let wait = minutes == 1 ? "1 minute" : "\(minutes) minutes"
        return "When a Claude Code prompt has waited \(wait) · at most \(report.maxPerHour) an hour"
    }

    private func write(_ on: Bool) {
        pending = on
        failure = nil
        Task {
            failure = await model.setEscalation(enabled: on)
            pending = nil
        }
    }
}

/// What turning the approval calls off is said to mean, in the Overview and the menu alike.
enum ApprovalCallsWording {
    static let offWarning = "Keryx won't ring you about Claude Code prompts until you turn this back on."
}

/// A request in the Overview: what last happened to it, what it was, and when.
struct ApprovalRow: View {
    let request: ApprovalWording.Request

    var body: some View {
        let title = request.outcome
        let detail = request.summary
        LabeledContent {
            Text(ApprovalWording.time(request.latest.ts))
                .monospacedDigit()
        } label: {
            HStack(spacing: 6) {
                Text(title)
                    .layoutPriority(1)
                if let detail {
                    Text(detail)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .help(detail)
                }
            }
        }
    }
}

/// How the approval audit's events read in the app.
enum ApprovalWording {
    /// A title for the event, and the detail beside it: what `keryx approvals --json` puts in
    /// `what` — the request's summary (a command, a question), else a reason or an answer —
    /// as the session wrote it.
    static func describe(_ entry: ApprovalEntry) -> (String, String?) {
        (title(entry.event), detail(entry.what.trimmingCharacters(in: .whitespaces)))
    }

    /// The broker's audit events (`keryx/approvals/broker.py`), in words. An event this build
    /// does not know is shown as its name, humanised.
    static func title(_ event: String) -> String {
        switch event {
        case "started": "Started listening for prompts"
        case "raised": "Claude Code asked"
        case "escalated": "Called you"
        case "escalation_skipped": "Didn't call"
        case "escalation_failed": "Call failed"
        case "armed": "Read out the choices"
        case "keypad_unrecognised": "Key not recognised"
        case "settled": "Settled"
        case "verdict_discarded": "Answer came too late"
        case "resolve_fallback": "Answered at the keyboard"
        case "resolve_unmatched": "Answer matched nothing"
        default: Wording.capitalised(event.replacingOccurrences(of: "_", with: " "))
        }
    }

    private static func detail(_ what: String) -> String? {
        what.isEmpty ? nil : what
    }

    /// One prompt Claude Code raised, and everything that happened to it.
    struct Request: Identifiable, Equatable {
        let id: String
        /// Its events, newest first; never empty.
        let events: [ApprovalEntry]

        var latest: ApprovalEntry { events[0] }

        /// What happened to it, in a few words: its last event before it was settled —
        /// "Settled" alone says nothing about how.
        var outcome: String {
            ApprovalWording.title((events.first { $0.event != "settled" } ?? latest).event)
        }

        /// Not a prompt: the broker starting, and the like.
        var standalone: Bool { id.hasPrefix("event-") }

        /// What it was about: the request's summary, as its first event carries it.
        var summary: String? {
            events.last { !$0.what.trimmingCharacters(in: .whitespaces).isEmpty }?.what
        }
    }

    /// The audit's events (oldest first) gathered by request, the request with the newest
    /// event first. An event that belongs to no request (the broker starting) stands alone.
    static func requests(_ entries: [ApprovalEntry]) -> [Request] {
        var order: [String] = []
        var events: [String: [ApprovalEntry]] = [:]
        for (index, entry) in entries.enumerated() {
            let id = key(entry.requestId) ?? "event-\(index)"
            if let at = order.firstIndex(of: id) { order.remove(at: at) }
            order.append(id)
            events[id, default: []].append(entry)
        }
        return order.reversed().map { Request(id: $0, events: (events[$0] ?? []).reversed()) }
    }

    private static func key(_ requestId: JSONValue?) -> String? {
        switch requestId {
        case nil, .null?: nil
        case let value?: "request-" + value.displayText
        }
    }

    /// The entry's time: the time alone today, else the date and time.
    static func time(_ ts: String, now: Date = .now) -> String {
        guard let date = date(ts) else { return ts }
        if Calendar.current.isDate(date, inSameDayAs: now) {
            return date.formatted(date: .omitted, time: .shortened)
        }
        return date.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }

    /// Parsed with the decoder every CLI document is read with, so the formats agree.
    static func date(_ ts: String) -> Date? {
        guard let data = try? JSONEncoder().encode(ts) else { return nil }
        return try? JSON.decoder.decode(Date.self, from: data)
    }
}

// MARK: - Memory

private struct MemorySection: View {
    let status: StatusReport

    @Environment(AppModel.self) private var model

    var body: some View {
        Section("Memory") {
            LabeledContent("What \(status.assistant.name) remembers") {
                if let memory = model.memory.value, memory.exists {
                    HStack(spacing: 10) {
                        Text((memory.path as NSString).lastPathComponent)
                            .foregroundStyle(.secondary)
                        Button("Reveal in Finder") {
                            NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: memory.path)])
                        }
                    }
                } else if model.memory.value != nil {
                    Text("Nothing yet — \(status.assistant.name) starts it after the first call")
                        .foregroundStyle(.secondary)
                } else {
                    Text((status.paths.memory as NSString).lastPathComponent)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}
