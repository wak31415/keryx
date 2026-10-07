import Foundation

public struct CommandRequest: Sendable, Equatable {
    public var executable: String // absolute path
    public var arguments: [String]
    public var environment: [String: String]
    public var workingDirectory: String?
    public var stdin: Data?
    public var timeout: Duration? // nil = no timeout

    public init(
        executable: String,
        arguments: [String] = [],
        environment: [String: String] = [:],
        workingDirectory: String? = nil,
        stdin: Data? = nil,
        timeout: Duration? = .seconds(30)
    ) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
        self.workingDirectory = workingDirectory
        self.stdin = stdin
        self.timeout = timeout
    }
}

public struct CommandResult: Sendable, Equatable {
    public var exitCode: Int32
    public var stdout: Data
    public var stderr: Data

    public var stdoutText: String { String(decoding: stdout, as: UTF8.self) }
    public var stderrText: String { String(decoding: stderr, as: UTF8.self) }

    public init(exitCode: Int32, stdout: Data, stderr: Data) {
        self.exitCode = exitCode
        self.stdout = stdout
        self.stderr = stderr
    }
}

public enum RunnerError: Error, Equatable {
    case launchFailed(String)
    case timedOut
}

public protocol CommandRunner: Sendable {
    /// Runs the child to its end. Cancelled, or past `request.timeout`, the child's whole
    /// process group is stopped (SIGTERM, then SIGKILL if it is still there a few seconds
    /// later) and waited for: a cancelled run returns what the child said on the way out, a
    /// timed-out one throws `RunnerError.timedOut`.
    func run(_ request: CommandRequest) async throws -> CommandResult

    /// Yields stdout lines as they arrive, and finishes normally once the child has exited
    /// and been reaped, whatever its exit code was — the caller interprets the terminal JSON
    /// event, not this stream's completion. Only a launch failure throws. `stop()` stops the
    /// child's process group as `run` does on cancellation, and the stream runs on to that
    /// end, so a caller that must know the child is gone keeps reading; cancelling the
    /// consuming task stops it too, without waiting. `request.timeout` is not applied here —
    /// a stream has no natural end time, and the caller decides when to stop it.
    func lines(_ request: CommandRequest) -> LineStream
}

/// A stream that can be asked to stop without being walked away from. `stop()` asks the
/// producer to wind down — for a child process, a SIGTERM to its process group — and the
/// elements go on until it has, so the consumer sees the real ending. Cancelling the
/// consuming task ends the iteration at once instead.
public struct StoppableStream<Element: Sendable>: AsyncSequence, Sendable {
    public typealias AsyncIterator = AsyncThrowingStream<Element, Error>.Iterator

    private let stream: AsyncThrowingStream<Element, Error>
    private let onStop: @Sendable () -> Void

    /// `stop`: what `stop()` does; by default nothing, for a stream that ends by itself.
    public init(_ stream: AsyncThrowingStream<Element, Error>, stop: @escaping @Sendable () -> Void = {}) {
        self.stream = stream
        onStop = stop
    }

    public func stop() { onStop() }

    public func makeAsyncIterator() -> AsyncIterator { stream.makeAsyncIterator() }
}

/// A child's stdout, line by line.
public typealias LineStream = StoppableStream<String>
