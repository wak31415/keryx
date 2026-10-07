import Foundation
import KeryxKit

/// The sample documents under `Sources/KeryxPreview/Preview/`: the CLI's own `--json` output,
/// captured from a seeded sandbox store with its paths scrubbed to `/Users/you`, edited by
/// hand only for a state no sandbox has (a running service, a running llama-server). They
/// are bundled resources, read through `Bundle.module`, so previews, snapshots and this
/// package's own tests all read the one copy rather than three.
///
/// A fixture failing to load or decode is this package's own bug, not a caller's mistake —
/// so every accessor crashes with a clear message instead of returning an optional or
/// throwing, the same stance Swift's own `Bundle.module` takes on a resource it expects to
/// find.
public enum PreviewData {
    public static func data(_ name: String) -> Data {
        guard let url = Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Preview") else {
            fatalError("PreviewData: no such fixture 'Preview/\(name)'")
        }
        guard let data = try? Data(contentsOf: url) else {
            fatalError("PreviewData: could not read fixture 'Preview/\(name)'")
        }
        return data
    }

    /// `pull.jsonl` as its individual lines, for feeding one at a time to `PullEvent.parse`.
    public static var pullLines: [String] {
        String(decoding: data("pull.jsonl"), as: UTF8.self)
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map(String.init)
    }

    public static var status: StatusReport { decode("status.json") }
    public static var statusMigration: StatusReport { decode("status-migration.json") }
    public static var doctor: DoctorReport { decode("doctor.json") }
    public static var tasks: TaskList { decode("tasks.json") }
    public static var taskDetail: TaskDetail { decode("task-detail.json") }
    public static var usage: UsageReport { decode("usage.json") }
    public static var models: ModelsReport { decode("models.json") }
    public static var modelsEmptyServers: ModelsReport { decode("models-empty-servers.json") }
    public static var config: [ConfigRow] { decode("config.json") }
    public static var approvals: ApprovalsReport { decode("approvals.json") }
    public static var memory: MemoryDocument { decode("memory.json") }
    public static var voice: VoiceStatus { decode("voice.json") }
    public static var voiceHere: VoiceStatus { decode("voice-here.json") }
    public static var voiceServer: VoiceStatus { decode("voice-server.json") }
    public static var voiceNeedsModel: VoiceStatus { decode("voice-needs-model.json") }

    /// `voice-here.jsonl` as its individual lines.
    public static var voiceHereLines: [String] { lines("voice-here.jsonl") }

    private static func lines(_ name: String) -> [String] {
        String(decoding: data(name), as: UTF8.self)
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map(String.init)
    }

    /// The `config options` documents there are fixtures for, by file, one per key. The
    /// voice, Claude, Codex, catalog and Kokoro lists are captured from a sandbox (the
    /// catalog's "fits" edited to match `models.json`'s Mac); the live OpenAI and
    /// model-server lists, which a sandbox with no key and no server cannot give, are
    /// written by hand.
    public static let optionsFixtures = [
        "options-realtime.json", "options-transcription.json", "options-voice.json",
        "options-subagent.json", "options-codex.json", "options-local-agent.json", "options-llm-server.json",
        "options-kokoro.json",
    ]

    /// The Realtime models with no OpenAI key to ask with: the built-in list and why,
    /// captured from a sandbox.
    public static var realtimeFallback: ConfigOptions { decode("options-realtime-fallback.json") }

    /// Each `config options` fixture, by key.
    public static var options: [String: ConfigOptions] {
        var byKey: [String: ConfigOptions] = [:]
        for name in optionsFixtures {
            let list: ConfigOptions = decode(name)
            byKey[list.key] = list
        }
        return byKey
    }

    /// What `config options` says for a key with no fixture: the built-in list, holding only
    /// the value the setting has in `config.json`.
    public static func builtinOptions(_ key: String) -> ConfigOptions {
        let current = config.first { $0.key == key }?.value.flatMap { value -> String? in
            if case let .string(text) = value, !text.isEmpty { return text }
            return nil
        }
        return ConfigOptions(key: key, current: current,
                             options: current.map { [ConfigOption(value: $0, label: $0, detail: "current")] } ?? [],
                             source: "builtin", live: false, freeForm: true, error: nil)
    }

    private static func decode<T: Decodable>(_ name: String) -> T {
        do {
            return try JSON.decoder.decode(T.self, from: data(name))
        } catch {
            fatalError("PreviewData: could not decode 'Preview/\(name)' as \(T.self): \(error)")
        }
    }
}
