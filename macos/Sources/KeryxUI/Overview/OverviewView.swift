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
    /// Installing or uninstalling the service: the question asked, then the run under way.
    @State private var unitChange: ServiceWording.Change?
    @State private var changingUnit: ServiceWording.Change?

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
                HStack(spacing: 10) {
                    VStack(alignment: .trailing, spacing: 2) {
                        HStack(spacing: 6) {
                            StatusDot(service.tone)
                            Text(service.text)
                        }
                        .help(service.help)
                        if let hint = service.hint {
                            Text(hint)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                    }
                    if let changingUnit {
                        ProgressView().controlSize(.small)
                            .accessibilityLabel(changingUnit.progress)
                    } else if !status.service.installed, status.service.ok, model.location.value?.checkout != nil {
                        Button("Install…") { unitChange = .install }
                            .help("Installs Keryx and its tunnel as services that start at login")
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
                                    .foregroundStyle(.orange)
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
                        Text("Failed").foregroundStyle(.red)
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
                } else if let changingUnit {
                    ProgressView().controlSize(.small)
                    Text(changingUnit.progress).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Reveal Logs") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: status.paths.logs)])
                }
                if status.service.installed {
                    Button("Uninstall…") { unitChange = .uninstall }
                        .disabled(restarting || changingUnit != nil)
                }
                Button("Restart…") { confirmingRestart = true }
                    .disabled(restarting || changingUnit != nil || !status.service.installed)
            }
        }
        .confirmationDialog(unitChange?.question ?? "", isPresented: Binding(
            get: { unitChange != nil }, set: { if !$0 { unitChange = nil } }
        ), presenting: unitChange) { change in
            Button(change.button, role: change == .uninstall ? .destructive : nil) { run(change) }
        } message: { change in
            Text(change.message(assistant: Wording.assistant(status) ?? "The assistant",
                                liveCalls: status.health.liveSessions ?? 0))
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
        status.health.reachable ? "Keryx is running" : model.menuState.headline
    }

    /// "Lyra · Claude": the assistant, and the agent it sends work to.
    private var byline: String {
        [status.assistant.name, Wording.agent(status.agents.default)]
            .filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// The service in a word, a line under it when there is more to say, and the manager
    /// and unit in its tooltip.
    private var service: (tone: StatusDot.Tone, text: String, hint: String?, help: String) {
        let s = status.service
        let manager = [s.manager ?? "launchd", s.unit].compactMap { $0 }.joined(separator: ", ")
        if !s.ok {
            return (.warning, "Unknown", s.error.map(Wording.sentence), "")
        }
        if !s.installed {
            let hint = model.location.value?.checkout == nil ? "Install it with keryx setup in Terminal" : nil
            return (.inactive, "Not installed", hint, "Not installed as a \(s.manager ?? "launchd") service")
        }
        if s.active { return (.ok, "Running", nil, "Running under \(manager)") }
        return (.inactive, "Stopped", nil, "Stopped (\(manager))")
    }

    /// Installs or uninstalls Keryx and its tunnel (`keryx service install|uninstall`),
    /// asked first; never the model or voice server, which their own screens start.
    private func run(_ change: ServiceWording.Change) {
        guard changingUnit == nil else { return }
        changingUnit = change
        Task {
            let answer = change == .install ? await model.installService() : await model.uninstallService()
            changingUnit = nil
            if case let .failure(error) = answer { failure = error }
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
                ForEach(open.filter { $0.section == section }) { CheckRow(check: $0, titles: titles) }
            } label: {
                Text(Self.title(ofSection: section))
            }
        }
        if !passed.isEmpty {
            DisclosureGroup(isExpanded: binding("", default: false)) {
                ForEach(passed) { CheckRow(check: $0, titles: titles) }
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

    private func openSetup() {
        guard let location = model.location.value else { return }
        do {
            try TerminalLauncher.open(location, arguments: ["setup"])
        } catch {
            failure = (error as? KeryxError) ?? .terminal(error.localizedDescription)
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
    /// Settings' titles by key, to say a detail's keys in.
    var titles: [String: String] = [:]

    var body: some View {
        let detail = OverviewWording.detail(check.detail, titles: titles)
        Label {
            VStack(alignment: .leading, spacing: 2) {
                Text(check.displayTitle)
                if let path = detail.path {
                    Text((path as NSString).abbreviatingWithTildeInPath)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .help(path)
                        .textSelection(.enabled)
                }
                if !detail.text.isEmpty {
                    Text(detail.text)
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
