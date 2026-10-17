import Foundation

/// A subagent session, as `keryx tasks list|show --json` prints it. The app shows it as
/// an agent session, not a row in a task list — `log` and `resumable` are what make that visible in the app.
public struct TaskItem: Codable, Sendable, Equatable, Identifiable {
    public var id: Int
    public var status: TaskStatus
    public var description: String
    public var project: String?
    public var agent: String
    public var model: String?
    public var cwd: String?
    public var createdAt: Date
    public var startedAt: Date?
    public var finishedAt: Date?
    public var reportedAt: Date?
    public var told: Bool?
    public var `internal`: Bool
    public var needsRestart: Bool
    public var summary: String?
    public var error: String?
    public var reportPath: String?
    public var inputTokens: Int?
    public var outputTokens: Int?
    public var costUsd: Double?
    public var originChannel: String?
    public var callbackRequested: Bool
}

public enum TaskStatus: String, Codable, Sendable, CaseIterable, Equatable {
    case queued, running, done, failed, cancelled
}

public struct TaskList: Codable, Sendable, Equatable {
    public var tasks: [TaskItem]
}

/// One entry of a session's log, oldest first. `id` is not part of the JSON — the CLI emits
/// a plain array — so it is assigned from the entry's position when `TaskDetail` decodes the
/// array, which is the only place one is ever constructed from JSON.
public struct SessionLogEntry: Codable, Sendable, Equatable, Identifiable {
    public enum Kind: String, Codable, Sendable, Equatable {
        case progress, followup
    }

    public var id: Int = 0
    public var time: String?
    public var kind: Kind
    public var text: String

    private enum CodingKeys: String, CodingKey {
        case time, kind, text
    }

    public init(id: Int, time: String?, kind: Kind, text: String) {
        self.id = id
        self.time = time
        self.kind = kind
        self.text = text
    }
}

/// `keryx tasks show ID --json`. `log`, `logTruncated` and `resumable` are additive: an
/// older CLI build (or a fixture written before they existed) simply omits them, and they
/// default to an empty log, not truncated, not resumable.
public struct TaskDetail: Codable, Sendable, Equatable {
    public var task: TaskItem
    public var report: String?
    public var reportTruncated: Bool
    public var log: [SessionLogEntry]
    public var logTruncated: Bool
    public var resumable: Bool

    private enum CodingKeys: String, CodingKey {
        case task, report, reportTruncated, log, logTruncated, resumable
    }

    public init(
        task: TaskItem, report: String?, reportTruncated: Bool, log: [SessionLogEntry],
        logTruncated: Bool, resumable: Bool
    ) {
        self.task = task
        self.report = report
        self.reportTruncated = reportTruncated
        self.log = log
        self.logTruncated = logTruncated
        self.resumable = resumable
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        task = try container.decode(TaskItem.self, forKey: .task)
        report = try container.decodeIfPresent(String.self, forKey: .report)
        reportTruncated = try container.decodeIfPresent(Bool.self, forKey: .reportTruncated) ?? false
        let decodedLog = try container.decodeIfPresent([SessionLogEntry].self, forKey: .log) ?? []
        log = decodedLog.enumerated().map { index, entry in
            var positioned = entry
            positioned.id = index
            return positioned
        }
        logTruncated = try container.decodeIfPresent(Bool.self, forKey: .logTruncated) ?? false
        resumable = try container.decodeIfPresent(Bool.self, forKey: .resumable) ?? false
    }
}
