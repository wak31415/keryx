import Foundation

/// Runs real child processes. `Process`, `Pipe` and `FileHandle` are not `Sendable`, so each
/// invocation confines them to one function, and any value that must be reached from a
/// `terminationHandler`, a `DispatchWorkItem`, or a cancellation handler — all of which
/// Foundation/Dispatch may invoke from another thread — is wrapped in `Box`, whose own
/// `Sendable` conformance is asserted once, here, rather than claimed for `Process` itself.
public final class ProcessRunner: CommandRunner, Sendable {
    /// A test hook: handed each `run`'s collector, so a test can hold it weakly and see that
    /// it is freed once the run is over.
    private let observeCollector: (@Sendable (AnyObject) -> Void)?

    public init() {
        observeCollector = nil
    }

    init(observingCollector: @escaping @Sendable (AnyObject) -> Void) {
        observeCollector = observingCollector
    }

    public func run(_ request: CommandRequest) async throws -> CommandResult {
        let process = Process()
        configure(process, with: request)

        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe

        var stdinPipe: Pipe?
        if request.stdin != nil {
            let pipe = Pipe()
            process.standardInput = pipe
            stdinPipe = pipe
        }

        let processBox = Box(process)

        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<CommandResult, Error>) in
                let collector = RunCollector(process: processBox, continuation: continuation)
                observeCollector?(collector)
                process.terminationHandler = { [weak collector] finished in
                    collector?.exited(finished.terminationStatus)
                }

                do {
                    try process.run()
                } catch {
                    collector.launchFailed(error.localizedDescription)
                    return
                }

                collector.start(reading: [stdoutPipe.fileHandleForReading,
                                          stderrPipe.fileHandleForReading])
                if let timeout = request.timeout {
                    collector.arm(timeout)
                }

                if let stdin = request.stdin, let stdinPipe {
                    stdinPipe.fileHandleForWriting.write(stdin)
                }
                try? stdinPipe?.fileHandleForWriting.close()
            }
        } onCancel: {
            if processBox.value.isRunning {
                processBox.value.terminate()
            }
        }
    }

    public func lines(_ request: CommandRequest) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            let process = Process()
            configure(process, with: request)

            let stdoutPipe = Pipe()
            process.standardOutput = stdoutPipe
            // Discarded, but not into a `Pipe` nobody reads: an unread pipe's buffer fills
            // (~64 KB) and blocks the child forever. `/dev/null` never fills.
            process.standardError = FileHandle.nullDevice

            var stdinPipe: Pipe?
            if request.stdin != nil {
                let pipe = Pipe()
                process.standardInput = pipe
                stdinPipe = pipe
            }

            do {
                try process.run()
            } catch {
                continuation.finish(throwing: RunnerError.launchFailed(error.localizedDescription))
                return
            }

            if let stdin = request.stdin, let stdinPipe {
                stdinPipe.fileHandleForWriting.write(stdin)
            }
            try? stdinPipe?.fileHandleForWriting.close()

            let processBox = Box(process)
            let handleBox = Box(stdoutPipe.fileHandleForReading)

            let task = Task {
                do {
                    for try await line in handleBox.value.bytes.lines {
                        continuation.yield(line)
                    }
                } catch {
                    // A handle closed out from under the reader (e.g. after `terminate()`)
                    // ends the stream quietly: `lines` finishes normally whatever happened,
                    // and the caller interprets the terminal JSON event, not this failure.
                }
                continuation.finish()
            }

            continuation.onTermination = { _ in
                task.cancel()
                if processBox.value.isRunning {
                    processBox.value.terminate()
                }
            }
        }
    }
}

private func configure(_ process: Process, with request: CommandRequest) {
    process.executableURL = URL(fileURLWithPath: request.executable)
    process.arguments = request.arguments
    // Set unconditionally: an empty dictionary means "exactly as given" (no environment at
    // all), not "inherit the parent's" — `Process.environment` left `nil` is what inherits.
    process.environment = request.environment
    if let workingDirectory = request.workingDirectory {
        process.currentDirectoryURL = URL(fileURLWithPath: workingDirectory)
    }
}

/// Carries one non-`Sendable` value (a `Process`, a `FileHandle`, a `Data` being
/// accumulated) across a boundary Foundation or Dispatch requires `Sendable` for —
/// `terminationHandler`, a `DispatchWorkItem`, `onCancel`, `onTermination`. Every value
/// held here is touched by exactly one closure at a time by construction (handed off, never
/// shared concurrently), so asserting `Sendable` is safe.
private final class Box<T>: @unchecked Sendable {
    var value: T
    init(_ value: T) { self.value = value }
}

/// One `run`: the two output streams, the exit, and the deadline, all confined to one serial
/// queue. The deadline bounds the whole wait, not only the child's life: a grandchild the
/// child left in the background (`cmd &` in a shell's rc files) can hold stdout or stderr
/// open long after the child has exited, and a reader blocked until EOF would wait for it.
/// So the streams are read by dispatch read sources rather than blocking reads, and on
/// expiry the child is terminated, each source cancelled (its cancel handler, which runs
/// after its last event, closes the handle), and `.timedOut` thrown.
///
/// Lifetime: the collector holds itself (`running`) from creation until it concludes, and
/// nothing else holds it strongly — the termination handler, the deadline and the sources'
/// handlers all capture it weakly. Every terminal path (finish, expiry, launch failure) goes
/// through `conclude`, which resumes the continuation exactly once and drops the
/// self-reference, the deadline and the sources, so a run never outlives its result.
private final class RunCollector: @unchecked Sendable {
    private let queue = DispatchQueue(label: "dev.keryx.app.process-runner")
    private let process: Box<Process>
    private var continuation: CheckedContinuation<CommandResult, Error>?
    private var running: RunCollector?
    private var sources: [DispatchSourceRead] = []
    private var output = [Data(), Data()]
    /// Streams not yet at EOF. Two from the start, so an exit reported before `start`
    /// cannot finish the run with nothing read.
    private var open = 2
    private var exitCode: Int32?
    private var deadline: DispatchWorkItem?
    private static let chunk = 65_536
    private var buffer = [UInt8](repeating: 0, count: RunCollector.chunk)

    init(process: Box<Process>, continuation: CheckedContinuation<CommandResult, Error>) {
        self.process = process
        self.continuation = continuation
        running = self
    }

    /// `handles[0]` is stdout and `handles[1]` stderr. Each handle is closed with
    /// `FileHandle.close()`, never a raw `close(2)`, so its own deinit does not close the
    /// descriptor a second time (by then possibly another file's).
    func start(reading handles: [FileHandle]) {
        queue.sync {
            for (index, handle) in handles.enumerated() {
                let box = Box(handle)
                let descriptor = handle.fileDescriptor
                let source = DispatchSource.makeReadSource(fileDescriptor: descriptor, queue: queue)
                source.setEventHandler { [weak self, weak source] in
                    guard let self else {
                        source?.cancel()
                        return
                    }
                    let count = Darwin.read(descriptor, &buffer, Self.chunk)
                    if count > 0 {
                        output[index].append(contentsOf: buffer[0..<count])
                    } else if count == 0 || (errno != EINTR && errno != EAGAIN) {
                        source?.cancel()
                    }
                }
                source.setCancelHandler { [weak self] in
                    // Closed whether or not the run is still waiting: the handle is the
                    // source's to close, once its last event has run.
                    try? box.value.close()
                    guard let self else { return }
                    open -= 1
                    finishIfDone()
                }
                sources.append(source)
                source.resume()
            }
        }
    }

    func arm(_ timeout: Duration) {
        let item = DispatchWorkItem { [weak self] in self?.expire() }
        queue.sync {
            // Already concluded (a run quicker than this call): nothing to arm.
            guard continuation != nil else { return }
            deadline = item
            queue.asyncAfter(deadline: .now() + timeout.timeInterval, execute: item)
        }
    }

    func exited(_ code: Int32) {
        queue.async { [weak self] in
            guard let self else { return }
            exitCode = code
            finishIfDone()
        }
    }

    func launchFailed(_ message: String) {
        queue.sync {
            conclude(.failure(RunnerError.launchFailed(message)))
        }
    }

    // MARK: On `queue`

    private func finishIfDone() {
        guard continuation != nil, let exitCode, open == 0 else { return }
        conclude(.success(CommandResult(exitCode: exitCode, stdout: output[0], stderr: output[1])))
    }

    private func expire() {
        guard continuation != nil else { return }
        if process.value.isRunning {
            process.value.terminate()
        }
        conclude(.failure(RunnerError.timedOut))
    }

    /// The one way out. Cancelling a source that has already finished does nothing; one
    /// still open is cancelled, and its cancel handler closes its handle.
    private func conclude(_ result: Result<CommandResult, Error>) {
        guard let continuation else { return }
        self.continuation = nil
        deadline?.cancel()
        deadline = nil
        sources.forEach { $0.cancel() }
        sources = []
        running = nil
        continuation.resume(with: result)
    }
}

extension Duration {
    var timeInterval: TimeInterval {
        let (seconds, attoseconds) = (components.seconds, components.attoseconds)
        return TimeInterval(seconds) + TimeInterval(attoseconds) / 1e18
    }
}
