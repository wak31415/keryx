import Foundation
import KeryxKit

/// A test double for `CommandRunner`. Every request passed to `run` or `lines` is recorded,
/// in call order, in `requests`. `@unchecked Sendable` is asserted because every mutable
/// field is read and written only behind `lock`.
public final class FakeRunner: CommandRunner, @unchecked Sendable {
    private let lock = NSLock()
    private var _requests: [CommandRequest] = []
    private var _responder: (CommandRequest) throws -> CommandResult
    private var _lineResponder: (CommandRequest) -> [String]
    private var _sessionResponder: ((CommandRequest) -> FakeSession)?

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

    /// What `interactiveLines` answers with: by default a session that says the
    /// `lineResponder`'s lines and ends.
    public var sessionResponder: ((CommandRequest) -> FakeSession)? {
        get { lock.lock(); defer { lock.unlock() }; return _sessionResponder }
        set { lock.lock(); _sessionResponder = newValue; lock.unlock() }
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

    public func interactiveLines(_ request: CommandRequest) -> LineStream {
        let (lines, session) = recordAndTakeSession(request)
        return (session?(request) ?? FakeSession(lines: lines(request), finishes: true)).stream()
    }

    private func recordAndTakeSession(
        _ request: CommandRequest
    ) -> ((CommandRequest) -> [String], ((CommandRequest) -> FakeSession)?) {
        lock.lock()
        defer { lock.unlock() }
        _requests.append(request)
        return (_lineResponder, _sessionResponder)
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

/// A child that takes input, scripted by a test: it says the lines it is given (now or
/// later), records what was sent to it and whether its input was closed or it was asked to
/// stop, and can answer each line sent (`onSend`) — a sign-in that says `done` once its
/// code arrives.
public final class FakeSession: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncThrowingStream<String, Error>.Continuation?
    private var pending: [String]
    private var finishesAtOnce: Bool
    private var _sent: [String] = []
    private var _inputClosed = false
    private var _stops = 0
    private var _onSend: (@Sendable (String, FakeSession) -> Void)?

    /// `lines`: said at once; `finishes`: then the stream ends.
    public init(lines: [String] = [], finishes: Bool = false) {
        pending = lines
        finishesAtOnce = finishes
    }

    /// Every line sent, in order.
    public var sent: [String] { lock.withLock { _sent } }
    public var inputClosed: Bool { lock.withLock { _inputClosed } }
    public var stopRequests: Int { lock.withLock { _stops } }

    /// Called with each line sent (and this session, to answer on).
    public var onSend: (@Sendable (String, FakeSession) -> Void)? {
        get { lock.withLock { _onSend } }
        set { lock.withLock { _onSend = newValue } }
    }

    public func yield(_ line: String) { lock.withLock { continuation }?.yield(line) }

    public func finish(throwing error: Error? = nil) {
        lock.withLock { continuation }?.finish(throwing: error)
    }

    public func stream() -> LineStream {
        let (stream, continuation) = AsyncThrowingStream<String, Error>.makeStream()
        let (lines, finishes) = lock.withLock {
            self.continuation = continuation
            return (pending, finishesAtOnce)
        }
        for line in lines { continuation.yield(line) }
        if finishes { continuation.finish() }
        let input = StreamInput(send: { [weak self] line in
            guard let self else { return false }
            let answer: (@Sendable (String, FakeSession) -> Void)?? = lock.withLock {
                guard !_inputClosed else { return .none }
                _sent.append(line)
                return .some(_onSend)
            }
            guard case let .some(answer) = answer else { return false }
            answer?(line, self)
            return true
        }, close: { [weak self] in
            guard let self else { return }
            lock.withLock { _inputClosed = true }
        })
        return LineStream(stream, stop: { [weak self] in
            guard let self else { return }
            lock.withLock {
                _stops += 1
                _inputClosed = true
            }
        }, input: input)
    }
}
