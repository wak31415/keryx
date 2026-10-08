import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

/// `Fixtures/contract.json` is what the app needs from each `keryx … --json` command: every
/// key every model here decodes, by path, with its JSON type ("|null" where the Swift
/// property is optional). `tests/test_macos_contract.py` holds the CLI to it; these tests
/// hold the Swift decoders to it, both ways:
///
/// - sufficient: each command's real fixture, cut down to the contract's keys, decodes —
///   and to exactly what the whole fixture decodes to, so no key the app reads was cut;
/// - complete: every stored property of every model a command decodes is in the contract,
///   with the type and nullability its Swift type has, and nothing else is.
@Suite struct ContractTests {
    // MARK: - The contract

    struct Contract: Sendable {
        /// Command line → path → key → type.
        var commands: [String: [String: [String: String]]]
        /// Command line → its stream of JSON lines.
        var streams: [String: Stream]
    }

    /// A command that prints JSON lines: each event's keys and types, the terminal events,
    /// and the events the exit codes are given for.
    struct Stream: Sendable {
        var events: [String: [String: String]]
        var terminal: [String]
        var exits: Set<String>
        var cancelledExit: Int?
    }

    static let contract: Contract = {
        let url = Bundle.module.url(forResource: "contract", withExtension: "json", subdirectory: "Fixtures")!
        let object = try! JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        precondition(object["schema"] as? Int == 2, "contract.json is not schema 2")
        let commands = object["commands"] as! [String: [String: [String: String]]]
        let streams = (object["streams"] as! [String: [String: Any]]).mapValues { stream in
            let exit = stream["exit"] as! [String: Any]
            return Stream(events: stream["events"] as! [String: [String: String]],
                          terminal: stream["terminal"] as! [String], exits: Set(exit.keys),
                          cancelledExit: exit["cancelled"] as? Int)
        }
        return Contract(commands: commands, streams: streams)
    }()

    static var pull: Stream { contract.streams["models pull --json"]! }
    static var voiceHere: Stream { contract.streams["voice use here --json"]! }

    /// Each command the app reads as JSON: its real fixtures, and how the app decodes it.
    struct Command: CustomTestStringConvertible, Sendable {
        var line: String
        var fixtures: [String]
        /// Decodes `full` and `cut`; true when both decode and are equal.
        var sameDecoded: @Sendable (Data, Data) throws -> Bool
        /// One decoded document, for its stored properties.
        var decoded: @Sendable (Data) throws -> Any
        var testDescription: String { line }

        static func of<T: Decodable & Equatable & Sendable>(_ line: String, _ type: T.Type, _ fixtures: [String]) -> Command {
            Command(
                line: line, fixtures: fixtures,
                sameDecoded: { full, cut in
                    try JSON.decoder.decode(T.self, from: full) == JSON.decoder.decode(T.self, from: cut)
                },
                decoded: { try JSON.decoder.decode(T.self, from: $0) })
        }
    }

    static let commands: [Command] = [
        .of("status --json", StatusReport.self, ["status.json", "status-migration.json"]),
        .of("doctor --json", DoctorReport.self, ["doctor.json"]),
        .of("tasks list --json", TaskList.self, ["tasks.json"]),
        .of("tasks show ID --json", TaskDetail.self, ["task-detail.json"]),
        .of("tasks usage --json", UsageReport.self, ["usage.json"]),
        .of("models list --json", ModelsReport.self, ["models.json", "models-empty-servers.json"]),
        .of("models use NAME --json", UseResult.self, ["fixture:use.json"]),
        .of("config list --json", [ConfigRow].self, ["config.json"]),
        .of("approvals --json", ApprovalsReport.self, ["approvals.json"]),
        .of("memory --json", MemoryDocument.self, ["memory.json"]),
        .of("config options KEY --json", ConfigOptions.self,
            PreviewData.optionsFixtures + ["options-realtime-fallback.json"]),
        .of("voice --json", VoiceStatus.self,
            ["voice.json", "voice-here.json", "voice-server.json", "voice-needs-model.json",
             "voice-here-own-llm.json"]),
        .of("voice use openai --json", VoiceUseResult.self, ["fixture:voice-use-openai.json"]),
        .of("voice use server URL --json", VoiceServerResult.self,
            ["fixture:voice-use-server.json", "fixture:voice-use-server-problem.json"]),
        .of("setup plan --json", SetupPlan.self,
            ["setup-plan.json", "setup-plan-partway.json", "setup-plan-ready.json"]),
        .of("setup mark STEP --json", SetupMarkResult.self, ["fixture:setup-mark.json"]),
        .of("pin --json", PinStatus.self, ["pin.json", "pin-file.json", "pin-locked.json", "pin-environment.json"]),
        .of("pin set --json", PinSetResult.self, ["fixture:pin-set.json"]),
        .of("config verify KEY --json", VerifyResult.self,
            ["fixture:verify-problem.json", "fixture:verify-unsaved.json", "fixture:verify-twilio.json"]),
        .of("phone numbers --json", PhoneNumbers.self, ["phone-numbers.json", "phone-numbers-unset.json"]),
        .of("phone webhooks --json", PhoneWebhooks.self,
            ["phone-webhooks.json", "phone-webhooks-unset.json", "phone-webhooks-matching.json"]),
        .of("phone webhooks --apply --json", WebhooksApplied.self, ["fixture:webhooks-apply.json"]),
        .of("phone tunnel --json", PhoneTunnel.self, ["phone-tunnel.json", "phone-tunnel-unset.json"]),
        .of("service --json", ServiceReport.self, ["service.json"]),
        .of("service install --json", ServiceResult.self, ["fixture:service-install.json"]),
        .of("service uninstall --json", ServiceResult.self, ["fixture:service-uninstall.json"]),
        .of("models stop --json", ServiceResult.self, ["fixture:models-stop.json"]),
        .of("integrations --json", IntegrationsReport.self, ["integrations.json"]),
        .of("auth logout gmail --json", SignOutResult.self, ["fixture:auth-logout-gmail.json"]),
        .of("auth logout google-workspace --json", SignOutResult.self,
            ["fixture:auth-logout-google-workspace.json"]),
        .of("plugins list --json", PluginsReport.self, ["plugins.json"]),
        .of("plugins install NAME --json", PluginResult.self, ["fixture:plugins-install.json"]),
        .of("plugins remove NAME --json", PluginResult.self, ["fixture:plugins-remove.json"]),
        .of("plugins set NAME --json", PluginResult.self, ["fixture:plugins-set.json"]),
        .of("tools --json", ToolsReport.self, ["tools.json"]),
        .of("plugins hosts --json", SshHostsReport.self, ["ssh-hosts.json"]),
        .of("setup profile --json", ProfileQuestions.self, ["setup-profile.json"]),
        .of("setup profile --preview --json", ProfileResult.self, ["fixture:setup-profile-preview.json"]),
        .of("setup profile --apply --json", ProfileResult.self,
            ["fixture:setup-profile-apply.json", "fixture:setup-profile-refused.json"]),
        .of("migrate --json", MigrationPlan.self,
            ["migrate-plan.json", "migrate-plan-nothing.json", "migrate-plan-conflict.json"]),
        .of("setup project-context --describe --json", ProjectContextQuestion.self, ["project-context.json"]),
        .of("setup project-context --apply --json", ProjectContextResult.self,
            ["fixture:project-context-apply.json"]),
    ]

    /// Keys the contract promises (and the CLI test holds the CLI to) that this build does
    /// not decode yet: the Configuration tabs, which Task UA's `ConfigRow` reads. Once a key
    /// is decoded the test says to drop it here.
    static let notYetDecoded: [String: [String: Set<String>]] = [:]

    /// Paths the app decodes whose keys the contract gives in prose, not by path, because
    /// the CLI test cannot step into them: a list that is often empty, an object that is
    /// often null (contract-handoff.md §1). The contract still names the key that holds them.
    static let inProse: [String: Set<String>] = [
        "integrations --json": ["integrations[].secrets[]"],
        "phone webhooks --json": ["current"],
        "tools --json": ["refused[]"],
        // Questions without choices have none (null); the problems and the moves are empty
        // whenever all is well, and `retire` is null when there is no `~/.jarvis`.
        "setup profile --json": ["questions[].choices[]"],
        "setup profile --preview --json": ["problems[]"],
        "setup profile --apply --json": ["problems[]"],
        "migrate --json": ["moves[]", "retire", "conflicts[]"],
        "setup project-context --apply --json": ["problems[]"],
    ]

    /// SEAMS: properties the app decodes, as optionals, that the CLI does not print yet
    /// (`Seams.swift`). Left out of the walk until it does; once a fixture holds one, the
    /// test below says to move it into the contract.
    static let awaitingCLI: Set<String> = [
        "PhoneTunnel.hostname",
        "SetupStep.deferrable", "SetupStep.deferred", "SetupMarkResult.deferred",
    ]

    /// A preview document (`Sources/KeryxPreview/Preview`), or `fixture:NAME` from this target's.
    static func data(_ name: String) throws -> Data {
        guard name.hasPrefix("fixture:") else { return PreviewData.data(name) }
        let url = try #require(Bundle.module.url(
            forResource: String(name.dropFirst("fixture:".count)), withExtension: nil, subdirectory: "Fixtures"))
        return try Data(contentsOf: url)
    }

    @Test func everyCommandTheAppReadsIsInTheContract() {
        #expect(Set(Self.contract.commands.keys) == Set(Self.commands.map(\.line)))
    }

    @Test func everyTypeIsOneTheContractKnows() {
        let known: Set<String> = ["string", "integer", "number", "boolean", "array", "object", "any"]
        for (line, paths) in Self.contract.commands {
            for (path, keys) in paths {
                for (key, type) in keys {
                    var parts = type.split(separator: "|").map(String.init)
                    if parts.last == "null" { parts.removeLast() }
                    #expect(parts.count == 1 && known.contains(parts[0]), "\(line) \(path).\(key): \(type)")
                }
            }
        }
    }

    // MARK: - Sufficient

    @Test(arguments: commands)
    func aFixtureCutToTheContractDecodesTheSame(_ command: Command) throws {
        let paths = try #require(Self.contract.commands[command.line])
        for name in command.fixtures {
            let full = try Self.data(name)
            let document = try JSONSerialization.jsonObject(with: full, options: .fragmentsAllowed)
            let cut = try JSONSerialization.data(withJSONObject: Self.cut(document, to: paths, at: ""))
            let same = try command.sameDecoded(full, cut)
            #expect(same, "\(name): the app reads a key the contract lacks")
        }
    }

    /// `node` with only the contract's keys at each path. An object or array the contract
    /// has no path for (a setting's value, `saved`, a list of names) is kept whole.
    static func cut(_ node: Any, to paths: [String: [String: String]], at path: String) -> Any {
        if let array = node as? [Any] {
            let element = path + "[]"
            guard paths[element] != nil else { return array }
            return array.map { cut($0, to: paths, at: element) }
        }
        guard let object = node as? [String: Any], let keys = paths[path] else { return node }
        var kept: [String: Any] = [:]
        for (key, value) in object where keys[key] != nil {
            kept[key] = cut(value, to: paths, at: path.isEmpty ? key : path + "." + key)
        }
        return kept
    }

    // MARK: - Complete

    @Test(arguments: commands)
    func everyKeyTheAppDecodesIsInTheContractWithItsType(_ command: Command) throws {
        let paths = try #require(Self.contract.commands[command.line])
        var swift: [String: [String: SwiftType]] = [:]
        for name in command.fixtures {
            let document = try command.decoded(Self.data(name))
            Self.walk(document, at: "", into: &swift)
        }
        let prose = Self.inProse[command.line] ?? []
        for path in Array(swift.keys) where prose.contains(where: { path == $0 || path.hasPrefix($0 + ".") }) {
            swift[path] = nil
        }
        #expect(prose.allSatisfy { paths[$0] == nil }, "\(command.line): a path in prose is in the contract too")
        #expect(Set(swift.keys) == Set(paths.keys), "paths")
        for (path, keys) in swift {
            var listed = paths[path] ?? [:]
            let pending = Self.notYetDecoded[command.line]?[path] ?? []
            #expect(pending.isDisjoint(with: keys.keys),
                    "\(command.line) \(path): decoded now, so drop it from notYetDecoded")
            for key in pending { listed[key] = nil }
            #expect(Set(keys.keys) == Set(listed.keys), "\(command.line) at \(path.isEmpty ? "the root" : path)")
            for (key, type) in keys {
                guard let written = listed[key] else { continue }
                #expect(type.matches(written), "\(command.line) \(path).\(key): Swift \(type), contract \(written)")
            }
        }
    }

    struct SwiftType: CustomStringConvertible {
        /// `nil`: any JSON (a `JSONValue`).
        var json: String?
        var optional: Bool

        var description: String { (json ?? "any") + (optional ? "|null" : "") }

        func matches(_ written: String) -> Bool {
            description == written
        }
    }

    /// Properties that are not decoded: `SessionLogEntry.id` is its position in the log.
    static let notDecoded: Set<String> = ["SessionLogEntry.id"]

    /// Records the stored properties of `value` (an object or an array of them) at `path`,
    /// as snake_case keys with the JSON type each needs, then those of each nested object.
    static func walk(_ value: Any, at path: String, into found: inout [String: [String: SwiftType]]) {
        if let array = value as? [Any] {
            for element in array where Mirror(reflecting: element).displayStyle == .struct {
                walk(element, at: path + "[]", into: &found)
            }
            return
        }
        let mirror = Mirror(reflecting: value)
        guard mirror.displayStyle == .struct else { return }
        let owner = String(describing: type(of: value))
        for child in mirror.children {
            guard let label = child.label, !notDecoded.contains(owner + "." + label),
                  !awaitingCLI.contains(owner + "." + label) else { continue }
            let key = snakeCase(label)
            found[path, default: [:]][key] = swiftType(of: child.value)
            let nested = path.isEmpty ? key : path + "." + key
            let unwrapped = unwrapOptional(child.value)
            if let unwrapped, unwrapped is [Any] {
                walk(unwrapped, at: nested, into: &found)
            } else if let unwrapped, Mirror(reflecting: unwrapped).displayStyle == .struct,
                      !(unwrapped is Date) {
                walk(unwrapped, at: nested, into: &found)
            }
        }
    }

    static func swiftType(of value: Any) -> SwiftType {
        if let optional = type(of: value) as? AnyOptional.Type {
            return SwiftType(json: jsonType(optional.wrapped), optional: true)
        }
        return SwiftType(json: jsonType(type(of: value)), optional: false)
    }

    static func jsonType(_ type: Any.Type) -> String? {
        switch type {
        case is String.Type, is Date.Type: "string"
        case is Int.Type: "integer"
        case is Double.Type: "number"
        case is Bool.Type: "boolean"
        case is JSONValue.Type: nil
        case is any RawRepresentable<String>.Type: "string"
        case is AnyArray.Type: "array"
        case is AnyDictionary.Type: "object"
        default: "object"
        }
    }

    static func unwrapOptional(_ value: Any) -> Any? {
        let mirror = Mirror(reflecting: value)
        guard mirror.displayStyle == .optional else { return value }
        return mirror.children.first?.value
    }

    static func snakeCase(_ camel: String) -> String {
        camel.reduce(into: "") { result, character in
            if character.isUppercase {
                result += "_" + character.lowercased()
            } else {
                result.append(character)
            }
        }
    }

    // MARK: - The pull stream

    @Test func theAppReadsTheseStreams() {
        #expect(Set(Self.contract.streams.keys) == Set(["models pull --json", "voice use here --json"]
            + Self.eventStreams.map(\.line)))
    }

    /// A line holding each of `keys` with a value of its type.
    static func line(_ event: String, _ keys: [String: String]) -> [String: Any] {
        var line: [String: Any] = ["event": event]
        for (key, type) in keys {
            line[key] = switch type.split(separator: "|").first {
            case "integer"?, "number"?: 7
            case "boolean"?: true
            case "array"?: [Any]()
            case "object"?: [String: Any]()
            default: "x"
            }
        }
        return line
    }

    // MARK: - Seams

    /// Every property a seam awaits is one a model really has, and nil in every fixture —
    /// the CLI does not print it yet. Once it does, the fixture is captured again and this
    /// fails: move the key into the contract and out of `awaitingCLI`.
    @Test(arguments: commands)
    func whatTheSeamsAwaitIsNotPrintedYet(_ command: Command) throws {
        for name in command.fixtures {
            let document = try command.decoded(Self.data(name))
            var found: [String] = []
            Self.seams(in: document, into: &found)
            #expect(found.isEmpty, "\(name) prints \(found): move them into the contract")
        }
    }

    @Test func eachSeamIsAPropertyTheAppDecodes() throws {
        var owners: Set<String> = []
        for command in Self.commands {
            for name in command.fixtures {
                Self.properties(in: try command.decoded(Self.data(name)), into: &owners)
            }
        }
        #expect(Self.awaitingCLI.isSubset(of: owners), "\(Self.awaitingCLI.subtracting(owners))")
    }

    /// The `Owner.label` of every stored property under `value`.
    static func properties(in value: Any, into found: inout Set<String>) {
        if let array = value as? [Any] {
            for element in array { properties(in: element, into: &found) }
            return
        }
        let mirror = Mirror(reflecting: value)
        guard mirror.displayStyle == .struct else { return }
        let owner = String(describing: type(of: value))
        for child in mirror.children {
            guard let label = child.label else { continue }
            found.insert(owner + "." + label)
            if let unwrapped = unwrapOptional(child.value) { properties(in: unwrapped, into: &found) }
        }
    }

    /// The seams under `value` that hold something.
    static func seams(in value: Any, into found: inout [String]) {
        if let array = value as? [Any] {
            for element in array { seams(in: element, into: &found) }
            return
        }
        let mirror = Mirror(reflecting: value)
        guard mirror.displayStyle == .struct else { return }
        let owner = String(describing: type(of: value))
        for child in mirror.children {
            guard let label = child.label else { continue }
            let unwrapped = unwrapOptional(child.value)
            if awaitingCLI.contains(owner + "." + label), unwrapped != nil { found.append(owner + "." + label) }
            if let unwrapped { seams(in: unwrapped, into: &found) }
        }
    }

    @Test func everyPullEventTheContractListsParsesWithItsKeysAndNotWithoutThem() throws {
        let events = Self.pull.events
        #expect(Set(events.keys) == ["progress", "done", "adopted", "error", "cancelled"])
        for (event, keys) in events {
            var line: [String: Any] = ["event": event]
            for (key, type) in keys { line[key] = type == "integer" ? 7 : "x" }
            let parsed = try #require(PullEvent.parse(Self.line(line)), "\(event)")
            #expect(Self.name(of: parsed) == event)
            // A key that may be null may be missing too (an older CLI's error has no detail).
            for key in keys.keys where !keys[key]!.hasSuffix("|null") {
                var without = line
                without.removeValue(forKey: key)
                #expect(try PullEvent.parse(Self.line(without)) == nil, "\(event) without \(key)")
            }
        }
    }

    @Test func theTerminalEventsAreTheOnesThatEndADownload() {
        let terminal = Self.pull.terminal
        // Every event but progress ends a download (`CLIClient.pull`'s `concluded`).
        #expect(Set(terminal) == Set(Self.pull.events.keys).subtracting(["progress"]))
        #expect(Self.pull.exits == Set(terminal))
        #expect(Self.pull.cancelledExit == 130)
    }

    // MARK: - The voice setup stream

    @Test func everyVoiceSetupEventParsesWithItsKeysAndNotWithoutThem() throws {
        let events = Self.voiceHere.events
        #expect(Set(events.keys) == ["step", "progress", "done", "error", "cancelled"])
        for (event, keys) in events {
            let line = Self.line(event, keys)
            let parsed = try #require(VoiceSetupEvent.parse(Self.line(line)), "\(event)")
            #expect(Self.name(of: parsed) == event)
            // A key that may be null may be missing too.
            for key in keys.keys where !keys[key]!.hasSuffix("|null") {
                var without = line
                without.removeValue(forKey: key)
                #expect(try VoiceSetupEvent.parse(Self.line(without)) == nil, "\(event) without \(key)")
            }
        }
    }

    @Test func theTerminalEventsAreTheOnesThatEndASetup() {
        let terminal = Self.voiceHere.terminal
        // Every event but step and progress ends a setup (`VoiceSetupEvent.isTerminal`).
        #expect(Set(terminal) == Set(Self.voiceHere.events.keys).subtracting(["step", "progress"]))
        #expect(Self.voiceHere.exits == Set(terminal))
        #expect(Self.voiceHere.cancelledExit == 130)
        for line in PreviewData.voiceHereLines + PreviewData.voiceHereRefusedLines {
            let event = try? #require(VoiceSetupEvent.parse(line))
            #expect(event?.isTerminal == terminal.contains(event.map(Self.name(of:)) ?? ""))
        }
    }

    static func name(of event: VoiceSetupEvent) -> String {
        switch event {
        case .step: "step"
        case .progress: "progress"
        case .done: "done"
        case .error: "error"
        case .cancelled: "cancelled"
        }
    }

    static func line(_ object: [String: Any]) throws -> String {
        String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self)
    }

    static func name(of event: PullEvent) -> String {
        switch event {
        case .progress: "progress"
        case .done: "done"
        case .adopted: "adopted"
        case .error: "error"
        case .cancelled: "cancelled"
        }
    }

    // MARK: - The other streams (`StreamEvent`)

    /// A stream the app reads as `StreamEvent`s: how a line parses (its name, and whether
    /// it ends the stream), the fixtures that replay it, and the keys its one parser takes
    /// as missing although this command always says them (one `SignedIn` reads all four
    /// sign-ins; Codex's `open` alone carries a code, Claude's `done` alone `stored`).
    struct EventStream: CustomTestStringConvertible, Sendable {
        var line: String
        var parse: @Sendable (String) -> (name: String, terminal: Bool)?
        var fixtures: [[String]]
        var sometimes: Set<String> = []
        var testDescription: String { line }

        static func of<Done: StreamDone>(_ line: String, _ done: Done.Type, _ fixtures: [[String]],
                                         sometimes: Set<String> = []) -> EventStream {
            EventStream(line: line, parse: { text in
                StreamEvent<Done>.parse(text).map { ($0.name, $0.isTerminal) }
            }, fixtures: fixtures, sometimes: sometimes)
        }
    }

    static let eventStreams: [EventStream] = [
        .of("models start --json", ModelServerStarted.self,
            [PreviewData.modelsStartLines, PreviewData.modelsStartRefusedLines]),
        .of("models start --voice-llm --json", ModelServerStarted.self,
            [PreviewData.voiceLLMServerStartLines, PreviewData.voiceLLMServerRefusedLines]),
        // A CLI a round older says no `voice_restarted`: read as false.
        .of("voice llm here NAME --json", VoiceLLMChosen.self,
            [PreviewData.voiceLLMHereLines, PreviewData.voiceLLMRefusedLines], sometimes: ["done.voice_restarted"]),
        .of("voice llm shared --json", VoiceLLMChosen.self, [PreviewData.voiceLLMSharedLines],
            sometimes: ["done.voice_restarted"]),
        .of("voice llm remote URL --json", VoiceLLMChosen.self, [PreviewData.voiceLLMRemoteRefusedLines],
            sometimes: ["done.voice_restarted"]),
        .of("voice download --json", VoiceModelsDownloaded.self, [PreviewData.voiceDownloadLines]),
        .of("auth login claude --json", SignedIn.self, [PreviewData.signInLines(.claude)], sometimes: ["done.stored"]),
        .of("auth login codex --json", SignedIn.self, [PreviewData.signInLines(.codex)], sometimes: ["open.code"]),
        .of("auth login gmail --json", SignedIn.self, [PreviewData.signInLines(.gmail)]),
        .of("auth login google-workspace --json", SignedIn.self, [PreviewData.signInLines(.googleWorkspace)]),
        .of("migrate --yes --json", MigrationDone.self, [PreviewData.migrateLines, PreviewData.migrateConflictLines]),
        .of("setup project-context --json", ProjectDraft.self,
            [PreviewData.projectDraftLines, PreviewData.projectDraftFailedLines]),
    ]

    @Test(arguments: eventStreams)
    func everyEventTheContractListsParsesWithItsKeysAndNotWithoutThem(_ stream: EventStream) throws {
        let promise = try #require(Self.contract.streams[stream.line])
        for (event, keys) in promise.events {
            let line = Self.line(event, keys)
            let parsed = try #require(stream.parse(try Self.line(line)), "\(stream.line) \(event)")
            #expect(parsed.name == event)
            for key in keys.keys where !keys[key]!.hasSuffix("|null") {
                var without = line
                without.removeValue(forKey: key)
                let still = stream.parse(try Self.line(without))
                if stream.sometimes.contains(event + "." + key) {
                    #expect(still?.name == event, "\(stream.line) \(event) without \(key)")
                } else {
                    #expect(still == nil, "\(stream.line) \(event) without \(key)")
                }
            }
        }
    }

    @Test(arguments: eventStreams)
    func theTerminalEventsAreTheOnesThatEndAStream(_ stream: EventStream) throws {
        let promise = try #require(Self.contract.streams[stream.line])
        // `done`, `error` and `cancelled` end every one (`StreamEvent.isTerminal`).
        #expect(Set(promise.terminal) == ["done", "error", "cancelled"])
        #expect(promise.exits == Set(promise.terminal))
        #expect(promise.cancelledExit == 130)
        for event in promise.events.keys {
            let parsed = try #require(stream.parse(try Self.line(Self.line(event, promise.events[event]!))))
            #expect(parsed.terminal == promise.terminal.contains(event), "\(stream.line) \(event)")
        }
        // Each replay is events the contract names, ending once, at its end.
        for lines in stream.fixtures {
            let events = lines.compactMap(stream.parse)
            #expect(events.count == lines.count, "\(stream.line): a fixture line is no event")
            #expect(events.allSatisfy { promise.events[$0.name] != nil })
            #expect(events.filter(\.terminal).count == 1 && events.last?.terminal == true)
        }
    }

    // MARK: - The served model

    @Test func theModelServerSaysWhatItServes() {
        #expect(PreviewData.models.servers.llm.servedModel == "qwen3-coder-30b-a3b")
        #expect(PreviewData.models.servers.llm.servedModel == PreviewData.models.servers.llm.model)
        #expect(PreviewData.modelsEmptyServers.servers.llm.servedModel == nil)
    }
}

/// An `Optional` of any type, nil or not, and what it wraps.
private protocol AnyOptional { static var wrapped: Any.Type { get } }
extension Optional: AnyOptional { static var wrapped: Any.Type { Wrapped.self } }

private protocol AnyArray {}
extension Array: AnyArray {}
private protocol AnyDictionary {}
extension Dictionary: AnyDictionary {}
