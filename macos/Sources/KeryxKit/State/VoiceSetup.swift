import Foundation
import Observation

/// Setting up the voice server on this Mac: one `keryx voice use here --json` at a time,
/// read step by step, as `Downloads` reads a download.
///
/// `cancel` asks the stream to stop, and the phase reads `.cancelling` until the stream has
/// ended — until the CLI and everything it started (an installer, the server it was
/// starting) have exited — so another setup cannot start while the first is still going.
/// A cancelled setup leaves no phase; one that went wrong stays `.failed` until the next
/// starts or it is dismissed; one that worked stays `.finished`. While one runs, the Mac is
/// asked not to idle-sleep under it.
@Observable @MainActor
public final class VoiceSetup {
    public enum Phase: Equatable, Sendable {
        /// `step` is the CLI's phase (`install`, `configure`, `unit`, `start`), empty until
        /// the first is announced; `progress` only while the phase reports sizes.
        case running(step: String, message: String, progress: VoiceSetupEvent.Progress?)
        /// Asked to stop, and not yet stopped: nothing may start until it has.
        case cancelling
        case failed(String)
        case finished(baseURL: String)
    }

    /// What the first step says until the CLI has said anything.
    public nonisolated static let starting = "Starting…"

    public private(set) var phase: Phase?
    @ObservationIgnored private var run: UUID?
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var stop: (@Sendable () -> Void)?
    @ObservationIgnored private var stopping = false
    @ObservationIgnored private let beginActivity: @MainActor (String) -> @MainActor () -> Void

    public init(beginActivity: @escaping @MainActor (String) -> @MainActor () -> Void = Downloads.processActivity) {
        self.beginActivity = beginActivity
    }

    /// A setup is running (not stopping).
    public var isRunning: Bool {
        if case .running = phase { true } else { false }
    }

    /// A setup is running or still stopping: no other may start.
    public var isBusy: Bool {
        isRunning || phase == .cancelling
    }

    /// Starts setting up, unless a setup is already running or still stopping. `onFinish`
    /// runs once it has worked.
    public func start(voice: String?, client: KeryxClient, onFinish: @escaping @MainActor () -> Void) {
        guard !isBusy else { return }
        let run = UUID()
        self.run = run
        stopping = false
        phase = .running(step: "", message: Self.starting, progress: nil)
        let stream = client.voiceHere(voice: voice)
        stop = { stream.stop() }
        let endActivity = beginActivity("Setting up the voice server")
        task = Task { [weak self] in
            defer {
                endActivity()
                if self?.run == run {
                    self?.task = nil
                    self?.stop = nil
                }
            }
            let outcome = await Self.consume(stream) { step, message, progress in
                self?.update(run, to: .running(step: step, message: message, progress: progress))
            }
            guard let self else { return }
            switch outcome {
            case let .finished(url):
                update(run, to: .finished(baseURL: url))
                if self.run == run { onFinish() }
            case let .failed(message):
                // Stopped on purpose, the CLI's going is not a failure to show.
                update(run, to: stopping ? nil : .failed(message))
            case .cancelled:
                update(run, to: nil)
            case let .stillRunning(message):
                // Something of it outlived SIGKILL: said, never taken for stopped.
                update(run, to: .failed(message))
            }
        }
    }

    /// Stops the setup: the phase reads `.cancelling` until it has stopped, then goes.
    public func cancel() {
        guard isRunning else { return }
        phase = .cancelling
        stopping = true
        stop?()
    }

    /// Forgets a setup that failed or finished.
    public func dismiss() {
        guard !isBusy else { return }
        phase = nil
    }

    /// Waits for the setup under way to end. For tests.
    func settled() async {
        await task?.value
    }

    private enum Outcome { case finished(String), failed(String), cancelled, stillRunning(String) }

    /// The setup's outcome once its stream has ended: the terminal event says what it was,
    /// the stream's end that the CLI and all it started have exited.
    private static func consume(
        _ stream: VoiceSetupStream,
        step: @MainActor (String, String, VoiceSetupEvent.Progress?) -> Void
    ) async -> Outcome {
        var outcome: Outcome?
        do {
            for try await event in stream {
                guard outcome == nil else { continue }
                switch event {
                case let .step(name, message, progress): step(name, message, progress)
                case let .done(url): outcome = .finished(url)
                case let .error(message): outcome = .failed(message)
                case .cancelled: outcome = .cancelled
                }
            }
        } catch is CancellationError {
            return .cancelled
        } catch let error as RunnerError where error == .stillRunning {
            return .stillRunning(error.localizedDescription)
        } catch let error as KeryxError {
            return Task.isCancelled ? .cancelled : outcome ?? .failed(error.message)
        } catch {
            return Task.isCancelled ? .cancelled : outcome ?? .failed(error.localizedDescription)
        }
        return Task.isCancelled ? .cancelled : outcome ?? .failed(VoiceSetupEvent.stoppedWithoutSaying)
    }

    /// Sets the phase — only while `run` is still the setup, and never back to `.running`
    /// once it has been asked to stop.
    private func update(_ run: UUID, to phase: Phase?) {
        guard self.run == run else { return }
        if case .running = phase, stopping { return }
        self.phase = phase
    }
}
