import Foundation

/// The sample documents under `Sources/KeryxKit/Preview/`: the CLI's own `--json` output,
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

    private static func decode<T: Decodable>(_ name: String) -> T {
        do {
            return try JSON.decoder.decode(T.self, from: data(name))
        } catch {
            fatalError("PreviewData: could not decode 'Preview/\(name)' as \(T.self): \(error)")
        }
    }
}
