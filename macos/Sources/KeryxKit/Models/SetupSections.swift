import Foundation

// The three setup sections the app walks without a terminal, through the wizard's own code
// on the CLI side: "About you" (`keryx setup profile`), the move from Jarvis (`keryx
// migrate`), and project context (`keryx setup project-context`).

// MARK: - About you

/// `keryx setup profile --json`: the questions the terminal wizard's "About you" asks, in its
/// order, and the memory they would write. Nothing here is a setting: the answers become the
/// assistant's first memory.
public struct ProfileQuestions: Codable, Sendable, Equatable {
    /// The section's title ("About you").
    public var title: String
    /// The assistant's name, as the questions say it.
    public var assistant: String
    /// Where what is said here goes: the privacy note, said before the questions.
    public var note: String
    public var memory: ProfileMemory
    public var questions: [ProfileQuestion]

    /// The question with this id (`replace`, `uses`, `other`, `facts`).
    public func question(_ id: String) -> ProfileQuestion? { questions.first { $0.id == id } }
}

/// The memory "About you" writes: whether one exists, and how long it is against what a call
/// reads.
public struct ProfileMemory: Codable, Sendable, Equatable {
    public var exists: Bool
    public var chars: Int
    public var maxChars: Int
}

/// One question: `replace` (a yes or no, only when a memory exists), `uses` (several of
/// `choices`), `other` (a line of text) and `facts` (lines).
public struct ProfileQuestion: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var title: String
    /// The question as the wizard asks it, the assistant named.
    public var prompt: String
    public var optional: Bool
    /// `boolean`, `choices`, `text` or `lines`.
    public var input: String
    /// What `uses` offers; `nil` for the others. Not in the contract by path, since most
    /// questions have none: each is a `value` and a `label`.
    public var choices: [ProfileChoice]?
    /// The answer when nothing is said.
    public var `default`: JSONValue
}

public struct ProfileChoice: Codable, Sendable, Equatable, Identifiable {
    public var value: String
    public var label: String

    public var id: String { value }
}

/// What "About you" was told: the JSON object `setup profile --preview|--apply` reads on
/// stdin. Every key is optional to the CLI; an empty answer says nothing.
public struct ProfileAnswers: Codable, Sendable, Equatable {
    /// Replace the memory that exists; asked only when one does.
    public var replace: Bool
    /// The `uses` choices' values.
    public var uses: [String]
    public var other: String
    /// One thing per line.
    public var facts: [String]

    public init(replace: Bool = false, uses: [String] = [], other: String = "", facts: [String] = []) {
        self.replace = replace
        self.uses = uses
        self.other = other
        self.facts = facts
    }

    /// The document for stdin: one JSON object, on one line.
    public func json() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(self)
    }
}

/// `keryx setup profile --preview|--apply --json`: what the answers come to, and for
/// `--apply` whether it was written. A refusal (`status` `refused`) is an answer with its
/// `problems`, not an error.
public struct ProfileResult: Codable, Sendable, Equatable {
    /// `written`, `kept`, `unchanged` or `refused`.
    public var status: String
    public var written: Bool
    /// The memory as written; `nil` unless it was.
    public var text: String?
    public var chars: Int
    public var maxChars: Int
    /// Why not, one sentence per answer at fault. Not in the contract by path: it is empty
    /// whenever all is well.
    public var problems: [SetupProblem]
    /// The section is recorded as walked.
    public var walked: Bool

    public init(status: String, written: Bool, text: String?, chars: Int, maxChars: Int,
                problems: [SetupProblem], walked: Bool) {
        self.status = status
        self.written = written
        self.text = text
        self.chars = chars
        self.maxChars = maxChars
        self.problems = problems
        self.walked = walked
    }

    public var refused: Bool { status == "refused" }
}

/// One answer at fault: its question's id, and why, in a sentence.
public struct SetupProblem: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var message: String

    public init(id: String, message: String) {
        self.id = id
        self.message = message
    }
}

// MARK: - Moving Jarvis's files

/// `keryx migrate --json`: what moving Jarvis's files to Keryx's would do, touching nothing.
/// Conflicts are in the document, never an error.
public struct MigrationPlan: Codable, Sendable, Equatable {
    /// Nothing to move, and no units left to put in place.
    public var nothingToDo: Bool
    /// Something to do, nothing in the way, and no call it would cut off.
    public var ready: Bool
    /// Each file or folder that moves. Not in the contract by path: it is empty on a Mac with
    /// nothing to move.
    public var moves: [MigrationMove]
    /// The `.env` files whose settings are imported.
    public var imports: [String]
    /// `~/.jarvis`, renamed aside once everything has moved. Not in the contract by path,
    /// since it is so often null.
    public var retire: MigrationRetire?
    /// What stands in the way, each in a sentence. Not in the contract by path, as `moves`.
    public var conflicts: [MigrationConflict]
    public var service: MigrationService
    /// Calls going on now; `nil` when no service is installed to ask.
    public var liveCalls: Int?
    /// A migration before this one moved the files but did not put the service back in
    /// place: the sentence that says so, else `nil`.
    public var unitsPending: String?

    /// The moves that move something (not a socket removed, nor a duplicate left behind).
    public var moving: [MigrationMove] { moves.filter { $0.kind == "move" } }
}

public struct MigrationMove: Codable, Sendable, Equatable {
    public var source: String
    /// `nil` for a socket that is removed.
    public var destination: String?
    /// `move`, `remove` or `duplicate`.
    public var kind: String
}

public struct MigrationRetire: Codable, Sendable, Equatable {
    public var from: String
    public var to: String
}

public struct MigrationConflict: Codable, Sendable, Equatable {
    /// For a person.
    public var message: String
    /// The terminal's sentence, with paths: for Show Details.
    public var detail: String
}

/// What the migration does to the background service.
public struct MigrationService: Codable, Sendable, Equatable {
    /// The unit stopped first, while the files move.
    public var stops: String?
    /// Jarvis's units, retired.
    public var retires: [String]
    /// Keryx's unit, installed or re-rendered.
    public var installs: String?
}

/// `keryx migrate --yes --json`'s `done`.
public struct MigrationDone: StreamDone {
    /// What happened, in a sentence.
    public var summary: String
    /// There was nothing to move.
    public var nothingToDo: Bool
    /// The files moved but the background service is not in place again: migrating once
    /// more finishes it.
    public var unitsPending: Bool

    public init(summary: String, nothingToDo: Bool, unitsPending: Bool) {
        self.summary = summary
        self.nothingToDo = nothingToDo
        self.unitsPending = unitsPending
    }

    public init?(event: [String: Any]) {
        guard let summary = event["summary"] as? String,
              let nothing = Self.boolean(event["nothing_to_do"]),
              let pending = Self.boolean(event["units_pending"]) else { return nil }
        self.init(summary: summary, nothingToDo: nothing, unitsPending: pending)
    }

    /// A JSON boolean, never a number standing in for one.
    static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }

    public static let stoppedWithoutSaying = "Moving the files stopped without saying why."
}

public typealias MigrationEvent = StreamEvent<MigrationDone>
/// `keryx migrate --yes --json`. Once its first step has come it is not to be stopped: the
/// CLI holds its signals off until the files are moved, and a kill would leave it half done.
public typealias MigrationStream = StoppableStream<MigrationEvent>

// MARK: - Project context

/// `keryx setup project-context --describe --json`: the question, and what is kept already.
public struct ProjectContextQuestion: Codable, Sendable, Equatable {
    public var title: String
    /// The agent that would read the projects (`claude`), and its name ("Claude").
    public var agent: String
    public var agentName: String
    /// What it does and what is kept, said before the question.
    public var note: String
    public var question: String
    public var choices: [ProjectContextChoice]
    /// The briefs kept already, by project.
    public var summaries: [String]
    /// The projects Keryx knows.
    public var projects: [String]
    public var maxBriefChars: Int
    public var maxFacts: Int
}

public struct ProjectContextChoice: Codable, Sendable, Equatable, Identifiable {
    /// `explore`, `folders` or `no`.
    public var value: String
    public var label: String
    public var hint: String?

    public var id: String { value }
}

/// Where the draft looks: everywhere from the home folder, or only in the folders named.
public enum ProjectScope: Sendable, Equatable {
    case explore
    case folders([String])
}

/// `keryx setup project-context --json`'s `done`: the draft, for the owner to check. Nothing
/// is kept until `--apply`.
public struct ProjectDraft: StreamDone {
    public var summary: String
    public var projects: [DraftProject]
    /// Things about the owner, one each.
    public var facts: [String]
    /// Projects past the total a call reads, left out.
    public var dropped: [String]
    /// The folders read: handed back to `--apply`, which adds a new project only inside them.
    public var folders: [String]

    public init(summary: String, projects: [DraftProject], facts: [String], dropped: [String], folders: [String]) {
        self.summary = summary
        self.projects = projects
        self.facts = facts
        self.dropped = dropped
        self.folders = folders
    }

    public init?(event: [String: Any]) {
        guard let summary = event["summary"] as? String,
              let projects = event["projects"] as? [Any],
              let facts = event["facts"] as? [String],
              let dropped = event["dropped"] as? [String],
              let folders = event["folders"] as? [String] else { return nil }
        let drafted = projects.compactMap { ($0 as? [String: Any]).flatMap(DraftProject.init(object:)) }
        guard drafted.count == projects.count else { return nil }
        self.init(summary: summary, projects: drafted, facts: facts, dropped: dropped, folders: folders)
    }

    public static let stoppedWithoutSaying = "Reading your projects stopped without saying why."
}

/// One project's drafted brief.
public struct DraftProject: Sendable, Equatable, Identifiable {
    public var name: String
    public var path: String?
    public var summary: String
    /// Keryx knows the project already.
    public var known: Bool
    /// Keeping it adds the project to the ones Keryx knows.
    public var addsProject: Bool

    public var id: String { name }

    public init(name: String, path: String?, summary: String, known: Bool, addsProject: Bool) {
        self.name = name
        self.path = path
        self.summary = summary
        self.known = known
        self.addsProject = addsProject
    }

    init?(object: [String: Any]) {
        guard let name = object["name"] as? String, let summary = object["summary"] as? String,
              let known = MigrationDone.boolean(object["known"]),
              let adds = MigrationDone.boolean(object["adds_project"]) else { return nil }
        self.init(name: name, path: object["path"] as? String, summary: summary, known: known, addsProject: adds)
    }
}

public typealias ProjectDraftEvent = StreamEvent<ProjectDraft>
/// `keryx setup project-context --json --explore|--folder PATH…`, which can be asked to stop.
public typealias ProjectDraftStream = StoppableStream<ProjectDraftEvent>

/// What is kept of a draft: the JSON object `setup project-context --apply` reads on stdin —
/// the folders read, and the projects and facts the owner kept, summaries as edited.
public struct ProjectContextKeep: Encodable, Sendable, Equatable {
    public struct Project: Encodable, Sendable, Equatable {
        public var name: String
        public var path: String?
        public var summary: String

        public init(name: String, path: String?, summary: String) {
            self.name = name
            self.path = path
            self.summary = summary
        }

        // `path` is said as null rather than left out: the CLI reads either.
        public func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(name, forKey: .name)
            try container.encode(path, forKey: .path)
            try container.encode(summary, forKey: .summary)
        }

        enum CodingKeys: String, CodingKey { case name, path, summary }
    }

    public var folders: [String]
    public var projects: [Project]
    public var facts: [String]

    public init(folders: [String], projects: [Project], facts: [String]) {
        self.folders = folders
        self.projects = projects
        self.facts = facts
    }

    public func json() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(self)
    }
}

/// `keryx setup project-context --apply --json`: what was kept. A problem is an answer, not
/// an error: on one in the draft nothing is written.
public struct ProjectContextResult: Codable, Sendable, Equatable {
    /// The briefs written, by project.
    public var kept: [String]
    public var addedProjects: [String]
    public var factsAdded: Int
    /// Not in the contract by path: it is empty whenever all is well.
    public var problems: [SetupProblem]
    public var walked: Bool

    public init(kept: [String], addedProjects: [String], factsAdded: Int, problems: [SetupProblem], walked: Bool) {
        self.kept = kept
        self.addedProjects = addedProjects
        self.factsAdded = factsAdded
        self.problems = problems
        self.walked = walked
    }
}
