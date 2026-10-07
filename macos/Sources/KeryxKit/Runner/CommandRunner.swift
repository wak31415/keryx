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
    func run(_ request: CommandRequest) async throws -> CommandResult

    /// Yields stdout lines as they arrive, and finishes normally once the child exits,
    /// whatever its exit code was — the caller interprets the terminal JSON event, not this
    /// stream's completion. Only a launch failure throws. Cancelling the consuming task
    /// terminates the child promptly.
    func lines(_ request: CommandRequest) -> AsyncThrowingStream<String, Error>
}
