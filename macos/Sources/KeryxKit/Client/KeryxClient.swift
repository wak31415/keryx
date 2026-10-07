import Foundation

/// Everything the app asks of Keryx. `CLIClient` answers it by running the `keryx` CLI with
/// `--json`; it is a protocol so a faster transport can take over a hot path later, and so
/// views can be previewed against a fake.
public protocol KeryxClient: Sendable {
    func status() async throws -> StatusReport
    /// `ok == false` is an answer, not an error: what is missing is in the checks.
    func doctor() async throws -> DoctorReport
    func tasks(includeInternal: Bool, limit: Int) async throws -> [TaskItem]
    func task(id: Int) async throws -> TaskDetail
    func usage(days: Int) async throws -> UsageReport
    func models() async throws -> ModelsReport
    /// Progress of a download, as it happens. Cancelling the consumer stops the download.
    func pull(model: String) -> AsyncThrowingStream<PullEvent, Error>
    func use(model: String, defaultAgent: Bool) async throws -> UseResult
    /// Runs `CHECKOUT/scripts/install-launchd.sh --llm`, and returns what it printed.
    func installModelServer() async throws -> String
    func config() async throws -> [ConfigRow]
    func set(key: String, value: String) async throws
    /// The value goes on the child's stdin, never its argv or environment.
    func setSecret(key: String, value: String) async throws
    func unset(key: String) async throws
    func approvals(limit: Int) async throws -> ApprovalsReport
    func setEscalation(enabled: Bool) async throws
    func memory() async throws -> MemoryDocument
    func restart(force: Bool) async throws
}
