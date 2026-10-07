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
            // The status and the doctor are already polled; these two are read here.
            await model.refresh(.approvals)
            await model.refresh(.memory)
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

            LabeledContent("Service") {
                VStack(alignment: .trailing, spacing: 2) {
                    HStack(spacing: 6) {
                        StatusDot(service.tone)
                        Text(service.text)
                    }
                    if let hint = service.hint {
                        Text(hint)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
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
                            Text("Running \(running)")
                                .textSelection(.enabled)
                            if let pending = pendingVersion {
                                Text(pending)
                                    .font(.caption)
                                    .foregroundStyle(.orange)
                            }
                        }
                    } else {
                        // The stamp the last start wrote: it says nothing about now.
                        Text("Last ran \(running)")
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
            }

            if status.restart.state == "failed" {
                LabeledContent("Last restart") {
                    VStack(alignment: .trailing, spacing: 2) {
                        Text("Failed").foregroundStyle(.red)
                        if let reason = status.restart.failure, !reason.isEmpty {
                            Text(Wording.capitalised(reason))
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
                    .disabled(restarting || !status.service.installed)
            }
        }
        .confirmationDialog("Restart Keryx?", isPresented: $confirmingRestart) {
            Button("Restart") { restart(force: false) }
        } message: {
            Text("Keryx stops, starts again, and calls you when it's back.")
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
        status.health.reachable ? "Keryx is running" : model.menuState.headline
    }

    /// "Lyra · Claude · v0.1.0-3-g1ad6420".
    private var byline: String {
        var parts = [status.assistant.name, Wording.agent(status.agents.default)]
        if status.health.reachable, let running = status.version.running { parts.append(running) }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    private var service: (tone: StatusDot.Tone, text: String, hint: String?) {
        let s = status.service
        if !s.ok {
            return (.warning, "Unknown", s.error.map(Wording.capitalised))
        }
        if !s.installed {
            let hint = model.location.value?.checkout != nil
                ? "Install it with scripts/install-launchd.sh" : nil
            return (.inactive, "Not installed", hint)
        }
        if s.active { return (.ok, "Running under \(s.manager ?? "launchd")", nil) }
        return (.inactive, "Stopped", nil)
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
        return "Restart to load \(checkout)"
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
    @State private var expanded: Set<String>?
    @State private var failure: KeryxError?

    var body: some View {
        Section("Setup") {
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
                    Text(error.title)
                    Text(error.message).textSelection(.enabled)
                }
            } else {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Checking setup…").foregroundStyle(.secondary)
                }
            }
        }
        .alert(failure?.title ?? "", isPresented: Binding(
            get: { failure != nil }, set: { if !$0 { failure = nil } }
        )) {
            Button("OK") {}
        } message: {
            Text(failure?.message ?? "")
        }
    }

    @ViewBuilder private func needsAttention(_ doctor: DoctorReport) -> some View {
        let open = doctor.checks.filter { !$0.ok }
        let passed = doctor.checks.filter(\.ok)
        LabeledContent {
            Button("Open Setup in Terminal") { openSetup() }
                .disabled(model.location.value == nil)
        } label: {
            Label {
                Text(open.count == 1 ? "1 thing needs attention" : "\(open.count) things need attention")
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            }
        }
        ForEach(Self.sections(of: open), id: \.self) { section in
            DisclosureGroup(isExpanded: binding(section, default: true)) {
                ForEach(open.filter { $0.section == section }) { CheckRow(check: $0) }
            } label: {
                Text(Self.title(ofSection: section))
            }
        }
        if !passed.isEmpty {
            DisclosureGroup(isExpanded: binding("", default: false)) {
                ForEach(passed) { CheckRow(check: $0) }
            } label: {
                Text(passed.count == 1 ? "1 check passed" : "\(passed.count) checks passed")
            }
        }
    }

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

    private func openSetup() {
        guard let location = model.location.value else { return }
        do {
            try TerminalLauncher.open(location, arguments: ["setup"])
        } catch {
            failure = (error as? KeryxError) ?? .launch(error.localizedDescription)
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
        case "service": "Background Service"
        case "security": "Security"
        default: Wording.capitalised(section)
        }
    }
}

private struct CheckRow: View {
    let check: DoctorCheck

    var body: some View {
        Label {
            VStack(alignment: .leading, spacing: 2) {
                Text(check.displayTitle)
                if !check.detail.isEmpty {
                    Text(Wording.capitalised(check.detail))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
        } icon: {
            Image(systemName: symbol)
                .foregroundStyle(colour)
                .accessibilityLabel(stateLabel)
        }
        // Under the disclosure's title, not under its chevron.
        .padding(.leading, 12)
        .padding(.vertical, 2)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var symbol: String {
        switch check.state {
        case "ok": "checkmark.circle.fill"
        case "missing": "exclamationmark.circle"
        default: "xmark.octagon.fill"
        }
    }

    private var colour: Color {
        switch check.state {
        case "ok": .green
        case "missing": .orange
        default: .red
        }
    }

    private var stateLabel: String {
        switch check.state {
        case "ok": "Passed"
        case "missing": "Missing"
        default: "Failed"
        }
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
                    .foregroundStyle(.red)
                    .textSelection(.enabled)
            }
            if let report = model.approvals.value, !report.entries.isEmpty {
                ForEach(report.entries.suffix(5).reversed()) { ApprovalRow(entry: $0) }
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

struct ApprovalRow: View {
    let entry: ApprovalEntry

    var body: some View {
        let (title, detail) = ApprovalWording.describe(entry)
        LabeledContent {
            Text(ApprovalWording.time(entry.ts))
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
