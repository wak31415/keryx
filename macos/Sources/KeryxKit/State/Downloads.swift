import Foundation
import Observation

/// Model downloads, one at a time, each a `keryx models pull --json` read event by event.
///
/// A download is a `Task` consuming the client's stream; cancelling it ends the stream,
/// which terminates the child. The row goes when a download is cancelled (by `cancel`, by
/// the CLI's own `cancelled` event, or by the task being cancelled), stays as `.failed` when
/// it went wrong, and stays as `.finished` when it worked. While one runs, the Mac is asked
/// not to idle-sleep under it.
@Observable @MainActor
public final class Downloads {
    public enum Phase: Equatable, Sendable {
        case running(done: Int64, total: Int64)
        case failed(String)
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

    public var isRunning: Bool {
        active.values.contains { if case .running = $0 { true } else { false } }
    }

    /// Starts downloading `model`, unless a download is already running. `onFinish` runs
    /// once it has downloaded (or adopted a copy already on disk).
    public func start(_ model: String, client: KeryxClient, onFinish: @escaping @MainActor () -> Void) {
        guard !isRunning else { return }
        let run = UUID()
        runs[model] = run
        lastStarted = model
        active[model] = .running(done: 0, total: 0)
        let stream = client.pull(model: model)
        let endActivity = beginActivity("Downloading a model")
        tasks[run] = Task { [weak self] in
            defer {
                endActivity()
                self?.tasks[run] = nil
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
                update(model, run: run, to: .failed(message))
            case .cancelled:
                update(model, run: run, to: nil)
            }
        }
    }

    /// Stops the download of `model` and removes its row.
    public func cancel(_ model: String) {
        guard let run = runs[model] else { return }
        tasks[run]?.cancel()
        runs[model] = nil
        active[model] = nil
    }

    /// Waits for every download under way to end. For tests.
    func settled() async {
        for task in Array(tasks.values) { await task.value }
    }

    private enum Outcome { case finished, failed(String), cancelled }

    private static func consume(
        _ stream: AsyncThrowingStream<PullEvent, Error>,
        progress: @MainActor (Int64, Int64) -> Void
    ) async -> Outcome {
        do {
            for try await event in stream {
                switch event {
                case let .progress(done, total): progress(done, total)
                case .done, .adopted: return .finished
                case let .error(message): return .failed(message)
                case .cancelled: return .cancelled
                }
            }
        } catch is CancellationError {
            return .cancelled
        } catch let error as KeryxError {
            return Task.isCancelled ? .cancelled : .failed(error.message)
        } catch {
            return Task.isCancelled ? .cancelled : .failed(error.localizedDescription)
        }
        return Task.isCancelled ? .cancelled : .failed(stoppedWithoutSaying)
    }

    /// Sets (or, with `nil`, removes) `model`'s row — only while `run` is still its download.
    private func update(_ model: String, run: UUID, to phase: Phase?) {
        guard runs[model] == run else { return }
        active[model] = phase
        if phase == nil { runs[model] = nil }
    }
}
