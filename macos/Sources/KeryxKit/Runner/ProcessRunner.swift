import Foundation

/// Runs real child processes. `Process`, `Pipe` and `FileHandle` are not `Sendable`, so each
/// invocation confines them to one function, and any value that must be reached from a
/// `terminationHandler`, a `DispatchWorkItem`, or a cancellation handler — all of which
/// Foundation/Dispatch may invoke from another thread — is wrapped in `Box`, whose own
/// `Sendable` conformance is asserted once, here, rather than claimed for `Process` itself.
public final class ProcessRunner: CommandRunner, Sendable {
    public init() {}

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
                let state = RunState()
                let group = DispatchGroup()
                let stdoutResult = Box(Data())
                let stderrResult = Box(Data())
                let stdoutHandle = Box(stdoutPipe.fileHandleForReading)
                let stderrHandle = Box(stderrPipe.fileHandleForReading)

                group.enter()
                DispatchQueue.global().async {
                    stdoutResult.value = stdoutHandle.value.readDataToEndOfFile()
                    group.leave()
                }
                group.enter()
                DispatchQueue.global().async {
                    stderrResult.value = stderrHandle.value.readDataToEndOfFile()
                    group.leave()
                }

                process.terminationHandler = { finished in
                    state.timeoutWorkItem?.cancel()
                    let exitCode = finished.terminationStatus
                    group.notify(queue: .global()) {
                        guard state.markResumed() else { return }
                        if state.timedOut {
                            continuation.resume(throwing: RunnerError.timedOut)
                        } else {
                            continuation.resume(returning: CommandResult(
                                exitCode: exitCode,
                                stdout: stdoutResult.value,
                                stderr: stderrResult.value
                            ))
                        }
                    }
                }

                do {
                    try process.run()
                } catch {
                    guard state.markResumed() else { return }
                    continuation.resume(throwing: RunnerError.launchFailed(error.localizedDescription))
                    return
                }

                if let stdin = request.stdin, let stdinPipe {
                    stdinPipe.fileHandleForWriting.write(stdin)
                }
                try? stdinPipe?.fileHandleForWriting.close()

                if let timeout = request.timeout {
                    let workItem = DispatchWorkItem {
                        if processBox.value.isRunning {
                            state.timedOut = true
                            processBox.value.terminate()
                        }
                    }
                    state.timeoutWorkItem = workItem
                    DispatchQueue.global().asyncAfter(
                        deadline: .now() + timeout.timeInterval, execute: workItem)
                }
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
            process.standardError = Pipe() // drained by the OS buffer and discarded

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
    if !request.environment.isEmpty {
        process.environment = request.environment
    }
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

/// The run/timeout/resume bookkeeping shared between the termination handler, the launch
/// failure path and the timeout work item, guarded by a single lock.
private final class RunState: @unchecked Sendable {
    private let lock = NSLock()
    private var resumed = false
    private var _timedOut = false
    private var _timeoutWorkItem: DispatchWorkItem?

    var timedOut: Bool {
        get { lock.lock(); defer { lock.unlock() }; return _timedOut }
        set { lock.lock(); _timedOut = newValue; lock.unlock() }
    }

    var timeoutWorkItem: DispatchWorkItem? {
        get { lock.lock(); defer { lock.unlock() }; return _timeoutWorkItem }
        set { lock.lock(); _timeoutWorkItem = newValue; lock.unlock() }
    }

    /// Returns `true` the first time it is called, `false` on every call after — the guard
    /// that lets exactly one of {termination, launch failure} resume the continuation.
    func markResumed() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if resumed { return false }
        resumed = true
        return true
    }
}

extension Duration {
    var timeInterval: TimeInterval {
        let (seconds, attoseconds) = (components.seconds, components.attoseconds)
        return TimeInterval(seconds) + TimeInterval(attoseconds) / 1e18
    }
}
