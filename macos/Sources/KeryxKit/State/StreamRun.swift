import Foundation
import Observation

/// One long-running `keryx … --json` command at a time, read event by event: `models
/// start`, `voice download`, a sign-in. What `VoiceSetup` is for `voice use here`, for any
/// `StreamEvent` stream.
///
/// `cancel` asks the stream to stop, and the phase reads `.cancelling` until the stream has
/// ended — until the CLI and everything it started have exited — so another run cannot
/// start while the first is still going. A cancelled run leaves no phase; one that went
/// wrong stays `.failed` until the next starts or it is dismissed; one that worked stays
/// `.finished` with what its `done` said. While one runs, the Mac is asked not to
/// idle-sleep under it.
///
/// A sign-in waits on the owner part way: `Activity.link` is the page to open (with Codex's
/// code to type there), `Activity.wantsCode` that Claude's CLI waits for the code the
/// owner was shown, and `send(_:)` hands a line to the CLI's stdin — the code, or Gmail's
/// landing address. A line sent is never kept here.
@Observable @MainActor
public final class StreamRun<Done: StreamDone> {
    public enum Phase: Equatable, Sendable {
        case running(Activity)
        /// Asked to stop, and not yet stopped: nothing may start until it has.
        case cancelling
        case failed(StreamFailure)
        case finished(Done)
    }

    /// Where a running command has got to.
    public struct Activity: Equatable, Sendable {
        /// The CLI's step (`runtime`, `unit`, `start`, `check`; `install`, `download`), empty
        /// until the first is announced.
        public var step: String
        /// What the step is doing, in the CLI's words; `StreamRun.starting` until it says.
        public var message: String
        /// What a terminal adds to the step (a path), for Show Details only.
        public var detail: String?
        /// Bytes so far while the step downloads; `nil` between downloads.
        public var progress: Progress?
        /// The last `open`: the page the owner should open, once.
        public var link: Link?
        /// The CLI asked for a code (`code`), and none has been sent since.
        public var wantsCode: Bool

        public init(step: String = "", message: String = StreamRun.starting, detail: String? = nil,
                    progress: Progress? = nil, link: Link? = nil, wantsCode: Bool = false) {
            self.step = step
            self.message = message
            self.detail = detail
            self.progress = progress
            self.link = link
            self.wantsCode = wantsCode
        }
    }

    public struct Progress: Equatable, Sendable {
        public var done: Int64
        /// `nil` when the source does not say.
        public var total: Int64?

        public init(done: Int64, total: Int64?) {
            self.done = done
            self.total = total
        }

        /// Done over total, when the total is known (and not zero).
        public var fraction: Double? {
            guard let total, total > 0 else { return nil }
            return Double(min(done, total)) / Double(total)
        }
    }

    /// A page to open, and the code to type there when there is one.
    public struct Link: Equatable, Sendable {
        public var url: String
        public var code: String?

        public init(url: String, code: String? = nil) {
            self.url = url
            self.code = code
        }
    }

    /// What the first step says until the CLI has said anything.
    public nonisolated static var starting: String { "Starting…" }

    /// How much of its stdin a command reads while it runs.
    public enum Input: Sendable {
        /// None: closed as the run starts (`models start`, Codex's and Google's sign-ins).
        case none
        /// One line, then it is closed (Claude's code, Gmail's landing address), so a
        /// second `send` is refused rather than left waiting in a pipe nobody reads.
        case oneLine
    }

    public private(set) var phase: Phase?
    /// The step the latest run reached, kept once it has ended — so a failure can be shown
    /// at the step it stopped on. Empty until its first step.
    public private(set) var lastStep = ""
    /// Every step the latest run has announced, in the order it first did: a step a
    /// command takes only sometimes (a voice server restarted, an old server restored) is
    /// shown only once it has come.
    public private(set) var seenSteps: [String] = []
    @ObservationIgnored private var run: UUID?
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var stream: StoppableStream<StreamEvent<Done>>?
    @ObservationIgnored private var input: Input = .none
    @ObservationIgnored private var stopping = false
    @ObservationIgnored private let beginActivity: @MainActor (String) -> @MainActor () -> Void

    /// `beginActivity` takes a reason and returns what ends the activity (`Downloads`').
    public init(beginActivity: @escaping @MainActor (String) -> @MainActor () -> Void = Downloads.processActivity) {
        self.beginActivity = beginActivity
    }

    /// A run is going (not stopping).
    public var isRunning: Bool {
        if case .running = phase { true } else { false }
    }

    /// A run is going or still stopping: no other may start.
    public var isBusy: Bool {
        isRunning || phase == .cancelling
    }

    /// Where the run under way has got to; `nil` unless it is running.
    public var activity: Activity? {
        if case let .running(activity) = phase { activity } else { nil }
    }

    /// What the last run's `done` said, while it stays finished.
    public var result: Done? {
        if case let .finished(done) = phase { done } else { nil }
    }

    /// Why the last run went wrong, while it stays failed.
    public var failure: StreamFailure? {
        if case let .failed(message) = phase { message } else { nil }
    }

    /// Starts the stream `make` opens, unless a run is already going or still stopping;
    /// true when it started. `reason` is what the Mac is told it is kept awake for;
    /// `takes`, how much of its stdin the command reads (none, by default: closed at once).
    /// `onFinish` runs once it has worked, with what its `done` said.
    @discardableResult
    public func start(reason: String, takes: Input = .none, _ make: () -> StoppableStream<StreamEvent<Done>>,
                      onFinish: @escaping @MainActor (Done) -> Void = { _ in }) -> Bool {
        guard !isBusy else { return false }
        let run = UUID()
        self.run = run
        stopping = false
        lastStep = ""
        seenSteps = []
        input = takes
        phase = .running(Activity())
        let stream = make()
        if takes == .none { stream.closeInput() }
        self.stream = stream
        let endActivity = beginActivity(reason)
        task = Task { [weak self] in
            defer {
                endActivity()
                if self?.run == run {
                    self?.task = nil
                    self?.stream = nil
                }
            }
            let outcome = await Self.consume(stream) { event in
                self?.advance(run, with: event)
            }
            guard let self else { return }
            switch outcome {
            case let .finished(done):
                update(run, to: .finished(done))
                if self.run == run { onFinish(done) }
            case let .failed(message):
                // Stopped on purpose, the CLI's going is not a failure to show.
                update(run, to: stopping ? nil : .failed(message))
            case .cancelled:
                update(run, to: nil)
            case let .stillRunning(message):
                // Something of it outlived SIGKILL: said, never taken for stopped.
                update(run, to: .failed(StreamFailure(message)))
            }
        }
        return true
    }

    /// Hands one line to the CLI's stdin — a sign-in's code, or the address the browser
    /// landed on — and, for a run that takes one line, closes it after. False when nothing
    /// is running, the stream takes no input (or no more), or `line` holds a line break.
    /// The line is not kept.
    @discardableResult
    public func send(_ line: String) -> Bool {
        guard isRunning, let stream, stream.send(line) else { return false }
        if input == .oneLine { stream.closeInput() }
        if case var .running(activity) = phase, activity.wantsCode {
            activity.wantsCode = false
            phase = .running(activity)
        }
        return true
    }

    /// Stops the run: the phase reads `.cancelling` until it has stopped, then goes.
    public func cancel() {
        guard isRunning else { return }
        phase = .cancelling
        stopping = true
        stream?.stop()
    }

    /// Forgets a run that failed or finished.
    public func dismiss() {
        guard !isBusy else { return }
        phase = nil
    }

    /// Waits for the run under way to end. For tests.
    func settled() async {
        await task?.value
    }

    private enum Outcome { case finished(Done), failed(StreamFailure), cancelled, stillRunning(String) }

    /// The run's outcome once its stream has ended: the terminal event says what it was,
    /// the stream's end that the CLI and all it started have exited.
    private static func consume(
        _ stream: StoppableStream<StreamEvent<Done>>,
        advance: @MainActor (StreamEvent<Done>) -> Void
    ) async -> Outcome {
        var outcome: Outcome?
        do {
            for try await event in stream {
                guard outcome == nil else { continue }
                switch event {
                case .step, .progress, .open, .code: advance(event)
                case let .done(done): outcome = .finished(done)
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
        return Task.isCancelled ? .cancelled : outcome ?? .failed(StreamFailure(Done.stoppedWithoutSaying))
    }

    /// One event that is not an ending, folded into the activity — only while `run` is
    /// still the run and has not been asked to stop.
    private func advance(_ run: UUID, with event: StreamEvent<Done>) {
        guard self.run == run, !stopping, case var .running(activity) = phase else { return }
        switch event {
        case let .step(step, message, detail):
            if step != activity.step { activity.progress = nil }
            activity.step = step
            activity.message = message
            activity.detail = detail
        case let .progress(step, done, total):
            activity.step = step
            activity.progress = Progress(done: done, total: total)
        case let .open(url, code):
            activity.link = Link(url: url, code: code)
        case .code:
            activity.wantsCode = true
        case .done, .error, .cancelled:
            return
        }
        lastStep = activity.step
        if !activity.step.isEmpty, !seenSteps.contains(activity.step) { seenSteps.append(activity.step) }
        phase = .running(activity)
    }

    /// Sets the phase — only while `run` is still the run.
    private func update(_ run: UUID, to phase: Phase?) {
        guard self.run == run else { return }
        self.phase = phase
    }
}
