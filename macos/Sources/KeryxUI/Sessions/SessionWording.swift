import Foundation
import KeryxKit

/// How the Sessions screen says what it shows: which sessions the list holds, the footer,
/// the bylines and the status lines. Kept apart from the views so each can be tested.
enum SessionWording {
    /// The label `keryx tasks usage` gives Keryx's own housekeeping (`HOUSEKEEPING_LABEL` in
    /// `cli.py`); the app's `ProjectUsage` does not carry the CLI's `internal` flag.
    static let housekeepingLabel = "(housekeeping)"
    /// The window the model asks `keryx tasks usage` about (`AppModel.usageDays`).
    static let usageDays = 30

    // MARK: - The list

    /// The sessions the list shows: housekeeping only when asked for, and those the status
    /// filter lets through.
    static func visible(_ tasks: [TaskItem], filter: TaskStatus?, housekeeping: Bool) -> [TaskItem] {
        tasks.filter { (housekeeping || !$0.internal) && matches($0.status, filter: filter) }
    }

    /// Whether a session with `status` passes `filter`. "Running" also lets through the
    /// queued ones: both are sessions that have not finished.
    static func matches(_ status: TaskStatus, filter: TaskStatus?) -> Bool {
        guard let filter else { return true }
        if filter == .running { return status == .running || status == .queued }
        return status == filter
    }

    /// The choices of the status filter, in order (`nil` is All).
    static let filters: [TaskStatus?] = [nil, .running, .done, .failed]

    static func filterTitle(_ filter: TaskStatus?) -> String {
        switch filter {
        case nil: "All"
        case .running?: "Running"
        case .done?: "Done"
        case .failed?: "Failed"
        case .queued?: "Queued"
        case .cancelled?: "Cancelled"
        }
    }

    /// Why a filter shows nothing, in a sentence.
    static func noneMatching(_ filter: TaskStatus) -> String {
        switch filter {
        case .running, .queued: "Nothing is running right now."
        case .done: "No session has finished yet."
        case .failed: "No session has failed."
        case .cancelled: "No session was cancelled."
        }
    }

    /// "Last 30 days: 22 sessions · $47.77 estimated". The cost part is left out when no
    /// session was priced.
    static func footer(_ usage: UsageReport, housekeeping: Bool, locale: Locale = .autoupdatingCurrent) -> String {
        let rows = usage.projects.filter { housekeeping || $0.label != housekeepingLabel }
        let count = rows.reduce(0) { $0 + $1.tasks }
        let costs = rows.compactMap(\.costUsd)
        var text = "Last \(usageDays) days: \(count == 1 ? "1 session" : "\(count) sessions")"
        if !costs.isEmpty {
            text += " · \(Formatting.cost(costs.reduce(0, +), locale: locale)) estimated"
        }
        return text
    }

    /// "Claude · keryx · 2 hours ago", under a session in the list.
    static func rowByline(_ task: TaskItem, now: Date = .now, locale: Locale = .autoupdatingCurrent) -> String {
        var parts = [Wording.agent(task.agent)]
        if let project = task.project, !project.isEmpty { parts.append(project) }
        parts.append(Formatting.relative(task.createdAt, now: now, locale: locale))
        return parts.joined(separator: " · ")
    }

    // MARK: - The timeline

    /// "Claude (claude-opus-5-5) · keryx · started 2 hours ago · via phone", under the
    /// session's title; the model is left out when the CLI names none.
    static func headerByline(_ task: TaskItem, now: Date = .now, locale: Locale = .autoupdatingCurrent) -> String {
        var parts = [Wording.agent(task.agent)]
        if let model = task.model, !model.isEmpty { parts[0] += " (\(model))" }
        if let project = task.project, !project.isEmpty { parts.append(project) }
        if let started = task.startedAt {
            parts.append("started \(Formatting.relative(started, now: now, locale: locale))")
        } else {
            parts.append("asked \(Formatting.relative(task.createdAt, now: now, locale: locale))")
        }
        if let channel = channel(task.originChannel) { parts.append(channel) }
        return parts.joined(separator: " · ")
    }

    /// Where the owner asked: "via phone", "on this Mac".
    static func channel(_ origin: String?) -> String? {
        switch origin {
        case "phone": "via phone"
        case "local": "on this Mac"
        case nil, "": nil
        case let other?: "via \(other)"
        }
    }

    /// "Done in 15 minutes", "Failed after 8 minutes", "Running", "Waiting to start".
    static func statusTitle(_ task: TaskItem, locale: Locale = .autoupdatingCurrent) -> String {
        let took = duration(from: task.startedAt, to: task.finishedAt, locale: locale)
        switch task.status {
        case .queued: return "Waiting to start"
        case .running: return "Running"
        case .done: return took.map { "Done in \($0)" } ?? "Done"
        case .failed: return took.map { "Failed after \($0)" } ?? "Failed"
        case .cancelled: return took.map { "Cancelled after \($0)" } ?? "Cancelled"
        }
    }

    /// The SF Symbol for a session's status.
    static func symbol(_ status: TaskStatus) -> String {
        switch status {
        case .queued: "clock"
        case .running: "arrow.triangle.2.circlepath"
        case .done: "checkmark.circle"
        case .failed: "xmark.circle"
        case .cancelled: "minus.circle"
        }
    }

    /// How long a session ran, in one unit: "15 minutes", "under a minute".
    static func duration(from start: Date?, to end: Date?, locale: Locale = .autoupdatingCurrent) -> String? {
        guard let start, let end, end >= start else { return nil }
        let seconds = end.timeIntervalSince(start)
        if seconds < 60 { return "under a minute" }
        let formatter = DateComponentsFormatter()
        formatter.unitsStyle = .full
        formatter.maximumUnitCount = 1
        formatter.allowedUnits = [.day, .hour, .minute]
        var calendar = Calendar.current
        calendar.locale = locale
        formatter.calendar = calendar
        return formatter.string(from: seconds)
    }

    /// "48K in · 6.1K out · $1.84 estimated"; `nil` when nothing was measured.
    static func usage(_ task: TaskItem, locale: Locale = .autoupdatingCurrent) -> String? {
        var parts: [String] = []
        if let input = task.inputTokens { parts.append("\(tokens(input, locale: locale)) in") }
        if let output = task.outputTokens { parts.append("\(tokens(output, locale: locale)) out") }
        if let cost = task.costUsd { parts.append("\(Formatting.cost(cost, locale: locale)) estimated") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// A token count to two significant figures: "48K", "6.1K", "980".
    static func tokens(_ count: Int, locale: Locale = .autoupdatingCurrent) -> String {
        count.formatted(.number.notation(.compactName).precision(.significantDigits(1...2)).locale(locale))
    }

    /// A moment in the session: the time alone today, else the day and the time.
    static func time(_ date: Date, now: Date = .now) -> String {
        if Calendar.current.isDate(date, inSameDayAs: now) {
            return date.formatted(date: .omitted, time: .shortened)
        }
        return date.formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }

    // MARK: - The log

    /// One stretch of the timeline: the agent's steps between two follow-ups, or a follow-up.
    enum Stretch: Identifiable, Equatable {
        case steps([SessionLogEntry])
        case followup(SessionLogEntry)

        var id: Int {
            switch self {
            case let .steps(entries): entries.first?.id ?? -1
            case let .followup(entry): entry.id
            }
        }
    }

    /// The log, oldest first, with consecutive progress entries gathered into one stretch.
    static func stretches(_ log: [SessionLogEntry]) -> [Stretch] {
        var result: [Stretch] = []
        var steps: [SessionLogEntry] = []
        for entry in log {
            switch entry.kind {
            case .progress:
                steps.append(entry)
            case .followup:
                if !steps.isEmpty { result.append(.steps(steps)) }
                steps = []
                result.append(.followup(entry))
            }
        }
        if !steps.isEmpty { result.append(.steps(steps)) }
        return result
    }

    /// A stretch longer than this is folded to its last `foldedCount` steps.
    static let foldAbove = 8
    static let foldedCount = 5
}
