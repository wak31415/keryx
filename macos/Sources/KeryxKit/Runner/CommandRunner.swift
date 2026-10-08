import Foundation

public struct CommandRequest: Sendable, Equatable {
    public var executable: String // absolute path
    public var arguments: [String]
    public var environment: [String: String]
    public var workingDirectory: String?
    public var stdin: Data?
    public var timeout: Duration? // nil = no timeout
    /// A line stream reads stderr with stdout, in the order written, rather than
    /// discarding it: for a program that is not Keryx's and says its errors there (Homebrew).
    public var mergesStderr: Bool
    /// How long a stopped line stream is given between SIGTERM and SIGKILL, when it needs
    /// longer than the runner's usual grace: a command that puts things back as it is
    /// stopped (`voice llm`, which restores the words it was replacing). `nil`: the usual.
    public var stopGrace: Duration?

    public init(
        executable: String,
        arguments: [String] = [],
        environment: [String: String] = [:],
        workingDirectory: String? = nil,
        stdin: Data? = nil,
        timeout: Duration? = .seconds(30),
        mergesStderr: Bool = false,
        stopGrace: Duration? = nil
    ) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
        self.workingDirectory = workingDirectory
        self.stdin = stdin
        self.timeout = timeout
        self.mergesStderr = mergesStderr
        self.stopGrace = stopGrace
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

public enum RunnerError: Error, Equatable, LocalizedError {
    case launchFailed(String)
    case timedOut
    /// The child was stopped, and something in its process group was still there after
    /// SIGKILL and the wait that follows it — reported, never taken for stopped.
    case stillRunning

    public var errorDescription: String? {
        switch self {
        case let .launchFailed(message): message
        case .timedOut: "The command took too long, and was stopped."
        case .stillRunning:
            "The command was stopped, but something it started is still running. Check Activity Monitor."
        }
    }
}

public protocol CommandRunner: Sendable {
    /// Runs the child to its end. Cancelled, or past `request.timeout`, the child's whole
    /// process group is stopped (SIGTERM, then SIGKILL if any of it is still there a few
    /// seconds later) and waited for until none of it is left: a cancelled run returns what
    /// the child said on the way out, a timed-out one throws `RunnerError.timedOut`, and one
    /// whose group outlived SIGKILL throws `RunnerError.stillRunning`.
    func run(_ request: CommandRequest) async throws -> CommandResult

    /// Yields stdout lines as they arrive, and finishes normally once the child has exited
    /// and been reaped — and, when it was stopped, once its whole group is gone (or throws
    /// `RunnerError.stillRunning` if some of it outlived SIGKILL) — whatever its exit code was — the caller interprets the terminal JSON
    /// event, not this stream's completion. Only a launch failure throws. `stop()` stops the
    /// child's process group as `run` does on cancellation, and the stream runs on to that
    /// end, so a caller that must know the child is gone keeps reading; cancelling the
    /// consuming task stops it too, without waiting. `request.timeout` is not applied here —
    /// a stream has no natural end time, and the caller decides when to stop it.
    func lines(_ request: CommandRequest) -> LineStream

    /// `lines`, with the child's stdin kept open for the caller to write to: the stream's
    /// `send(_:)` writes one line to it, and its `closeInput()` closes it (as the stream's
    /// end does). `request.stdin`, when given, is written first and stdin stays open after
    /// it. Stopping and finishing are exactly `lines`'s. A runner that cannot give a child
    /// input answers with `lines` itself: its stream's `send` returns false.
    func interactiveLines(_ request: CommandRequest) -> LineStream
}

extension CommandRunner {
    public func interactiveLines(_ request: CommandRequest) -> LineStream { lines(request) }
}

/// A stream's way back to its producer: one line at a time, until it is closed. For a
/// child process, its stdin.
public struct StreamInput: Sendable {
    private let write: @Sendable (String) -> Bool
    private let onClose: @Sendable () -> Void

    /// `send`: takes one line (no newline in it) and says whether it was taken; `close`:
    /// no more lines, which the producer reads as the end of its input.
    public init(send: @escaping @Sendable (String) -> Bool, close: @escaping @Sendable () -> Void) {
        write = send
        onClose = close
    }

    /// Hands over `line` — false when it holds a line break (it would be two lines), or
    /// the input is closed. Nothing keeps the line once it is written.
    public func send(_ line: String) -> Bool {
        guard !line.contains(where: \.isNewline) else { return false }
        return write(line)
    }

    public func close() { onClose() }
}

/// A stream that can be asked to stop without being walked away from. `stop()` asks the
/// producer to wind down — for a child process, a SIGTERM to its process group — and the
/// elements go on until it has, so the consumer sees the real ending. Cancelling the
/// consuming task ends the iteration at once instead.
///
/// One with `input` can also be written to (`send`), for a command that asks for a line on
/// its stdin while it runs (a sign-in's code). One without takes nothing: `send` is false.
public struct StoppableStream<Element: Sendable>: AsyncSequence, Sendable {
    public typealias AsyncIterator = AsyncThrowingStream<Element, Error>.Iterator

    private let stream: AsyncThrowingStream<Element, Error>
    private let onStop: @Sendable () -> Void
    /// The producer's input, for a stream that takes any.
    public let input: StreamInput?

    /// `stop`: what `stop()` does; by default nothing, for a stream that ends by itself.
    public init(_ stream: AsyncThrowingStream<Element, Error>, stop: @escaping @Sendable () -> Void = {},
                input: StreamInput? = nil) {
        self.stream = stream
        onStop = stop
        self.input = input
    }

    public func stop() { onStop() }

    /// Whether `send` can ever take a line.
    public var acceptsInput: Bool { input != nil }

    /// Writes `line` and a newline to the producer's input (a child's stdin). False when
    /// the stream takes no input, its input is closed, or `line` holds a line break.
    @discardableResult
    public func send(_ line: String) -> Bool { input?.send(line) ?? false }

    /// No more input: the producer reads the end of it. Does nothing without input.
    public func closeInput() { input?.close() }

    public func makeAsyncIterator() -> AsyncIterator { stream.makeAsyncIterator() }
}

/// A child's stdout, line by line.
public typealias LineStream = StoppableStream<String>
