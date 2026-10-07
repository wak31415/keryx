import Foundation
import Observation

/// Model downloads, one at a time, each a `keryx models pull --json` read event by event.
///
/// A download is a `Task` consuming the client's stream. `cancel` asks the stream to stop
/// and the row reads `.cancelling` until the stream has ended — until the CLI and everything
/// it started have exited — so a second download of the same file cannot start while the
/// first is still writing it. The row goes when a download is cancelled (by `cancel`, by
/// the CLI's own `cancelled` event, or by the task being cancelled), stays as `.failed` when
/// it went wrong, and stays as `.finished` when it worked. While one runs, the Mac is asked
/// not to idle-sleep under it.
@Observable @MainActor
public final class Downloads {
    public enum Phase: Equatable, Sendable {
        case running(done: Int64, total: Int64)
        /// Asked to stop, and not yet stopped: nothing may start until it has.
        case cancelling
        case failed(StreamFailure)
        case finished
    }

    /// What a stream that ended with no terminal event becomes. It never counts as finished.
    static let stoppedWithoutSaying = PullEvent.stoppedWithoutSaying

    /// Model name → phase.
    public private(set) var active: [String: Phase] = [:]
    private var lastStarted: String?
    /// Each download's run, so a late update from a cancelled one cannot touch a newer row.
    @ObservationIgnored private var runs: [String: UUID] = [:]
    @ObservationIgnored private var tasks: [UUID: Task<Void, Never>] = [:]
    /// Each download's way to stop, while it runs.
    @ObservationIgnored private var stops: [UUID: @Sendable () -> Void] = [:]
    /// The runs `cancel` has asked to stop: whatever they end with, the row goes (unless
    /// the download finished first).
    @ObservationIgnored private var stopping: Set<UUID> = []
    @ObservationIgnored private let beginActivity: @MainActor (String) -> @MainActor () -> Void

    /// `beginActivity` takes a reason and returns what ends the activity; the default holds a
    /// `ProcessInfo` user-initiated activity.
    public init(beginActivity: @escaping @MainActor (String) -> @MainActor () -> Void = Downloads.processActivity) {
        self.beginActivity = beginActivity
    }

    public static func processActivity(_ reason: String) -> @MainActor () -> Void {
        let token = ProcessInfo.processInfo.beginActivity(options: .userInitiated, reason: reason)
        return { ProcessInfo.processInfo.endActivity(token) }
    }

    /// The download in progress, else the last one started while its row remains.
    public var current: (name: String, phase: Phase)? {
        if let running = active.first(where: { if case .running = $0.value { true } else { false } }) {
            return (running.key, running.value)
        }
        guard let name = lastStarted, let phase = active[name] else { return nil }
        return (name, phase)
    }

    /// A download is downloading (not stopping).
    public var isRunning: Bool {
        active.values.contains { if case .running = $0 { true } else { false } }
    }

    /// A download is downloading or still stopping: no other may start.
    public var isBusy: Bool {
        isRunning || active.values.contains(.cancelling)
    }

    /// Starts downloading `model`, unless a download is already running or still stopping.
    /// `onFinish` runs once it has downloaded (or adopted a copy already on disk).
    public func start(_ model: String, client: KeryxClient, onFinish: @escaping @MainActor () -> Void) {
        guard !isBusy else { return }
        let run = UUID()
        runs[model] = run
        lastStarted = model
        active[model] = .running(done: 0, total: 0)
        let stream = client.pull(model: model)
        stops[run] = { stream.stop() }
        let endActivity = beginActivity("Downloading a model")
        tasks[run] = Task { [weak self] in
            defer {
                endActivity()
                self?.tasks[run] = nil
                self?.stops[run] = nil
                self?.stopping.remove(run)
            }
            let outcome = await Self.consume(stream) { done, total in
                self?.update(model, run: run, to: .running(done: done, total: total))
            }
            guard let self else { return }
            switch outcome {
            case .finished:
                update(model, run: run, to: .finished)
                if runs[model] == run { onFinish() }
            case let .failed(message):
                // Stopped on purpose, the CLI's going is not a failure to show.
                update(model, run: run, to: stopping.contains(run) ? nil : .failed(message))
            case .cancelled:
                update(model, run: run, to: nil)
            case let .stillRunning(message):
                // Something of it outlived SIGKILL: said, never taken for stopped.
                update(model, run: run, to: .failed(StreamFailure(message)))
            }
        }
    }

    /// Stops the download of `model`: the row reads `.cancelling` until the download has
    /// stopped, then goes.
    public func cancel(_ model: String) {
        guard let run = runs[model], case .running = active[model] else { return }
        active[model] = .cancelling
        stopping.insert(run)
        stops[run]?()
    }

    /// Waits for every download under way to end. For tests.
    func settled() async {
        for task in Array(tasks.values) { await task.value }
    }

    private enum Outcome { case finished, failed(StreamFailure), cancelled, stillRunning(String) }

    /// The download's outcome, once its stream has ended. The terminal event says what it
    /// was, but the stream's end says it is over: for the CLI, that `keryx models pull` and
    /// everything it started have exited, so nothing is still writing the file. Until then
    /// the row stays as it is (running, or cancelling).
    private static func consume(
        _ stream: PullStream,
        progress: @MainActor (Int64, Int64) -> Void
    ) async -> Outcome {
        var outcome: Outcome?
        do {
            for try await event in stream {
                guard outcome == nil else { continue }
                switch event {
                case let .progress(done, total): progress(done, total)
                case .done, .adopted: outcome = .finished
                case let .error(message): outcome = .failed(message)
                case .cancelled: outcome = .cancelled
                }
            }
        } catch is CancellationError {
            return .cancelled
        } catch let error as RunnerError where error == .stillRunning {
            return .stillRunning(error.localizedDescription)
        } catch let error as KeryxError {
            return Task.isCancelled ? .cancelled : outcome ?? .failed(StreamFailure(error))
        } catch {
            return Task.isCancelled ? .cancelled : outcome ?? .failed(StreamFailure(error.localizedDescription))
        }
        return Task.isCancelled ? .cancelled : outcome ?? .failed(StreamFailure(stoppedWithoutSaying))
    }

    /// Sets (or, with `nil`, removes) `model`'s row — only while `run` is still its download,
    /// and never back to `.running` once it has been asked to stop.
    private func update(_ model: String, run: UUID, to phase: Phase?) {
        guard runs[model] == run else { return }
        if case .running = phase, stopping.contains(run) { return }
        active[model] = phase
        if phase == nil { runs[model] = nil }
    }
}
