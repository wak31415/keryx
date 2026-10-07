import Foundation

/// A test double for `CommandRunner`. Every request passed to `run` or `lines` is recorded,
/// in call order, in `requests`. `@unchecked Sendable` is asserted because every mutable
/// field is read and written only behind `lock`.
public final class FakeRunner: CommandRunner, @unchecked Sendable {
    private let lock = NSLock()
    private var _requests: [CommandRequest] = []
    private var _responder: (CommandRequest) throws -> CommandResult
    private var _lineResponder: (CommandRequest) -> [String]

    public init(
        responder: @escaping (CommandRequest) throws -> CommandResult = { _ in
            CommandResult(exitCode: 0, stdout: Data(), stderr: Data())
        },
        lineResponder: @escaping (CommandRequest) -> [String] = { _ in [] }
    ) {
        self._responder = responder
        self._lineResponder = lineResponder
    }

    public var requests: [CommandRequest] {
        lock.lock()
        defer { lock.unlock() }
        return _requests
    }

    public var responder: (CommandRequest) throws -> CommandResult {
        get { lock.lock(); defer { lock.unlock() }; return _responder }
        set { lock.lock(); _responder = newValue; lock.unlock() }
    }

    public var lineResponder: (CommandRequest) -> [String] {
        get { lock.lock(); defer { lock.unlock() }; return _lineResponder }
        set { lock.lock(); _lineResponder = newValue; lock.unlock() }
    }

    public func run(_ request: CommandRequest) async throws -> CommandResult {
        let respond = recordAndTakeResponder(request)
        return try respond(request)
    }

    public func lines(_ request: CommandRequest) -> LineStream {
        let respond = recordAndTakeLineResponder(request)
        return LineStream(AsyncThrowingStream { continuation in
            for line in respond(request) {
                continuation.yield(line)
            }
            continuation.finish()
        })
    }

    // `NSLock.lock()`/`unlock()` are unavailable called directly inside an `async`
    // function body (Swift wants scoped locking there); these plain synchronous helpers
    // are the scope.
    private func recordAndTakeResponder(
        _ request: CommandRequest
    ) -> (CommandRequest) throws -> CommandResult {
        lock.lock()
        defer { lock.unlock() }
        _requests.append(request)
        return _responder
    }

    private func recordAndTakeLineResponder(
        _ request: CommandRequest
    ) -> (CommandRequest) -> [String] {
        lock.lock()
        defer { lock.unlock() }
        _requests.append(request)
        return _lineResponder
    }
}
