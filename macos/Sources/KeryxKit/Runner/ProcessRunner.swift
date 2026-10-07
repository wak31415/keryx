import Darwin
import Foundation

/// Runs real child processes, each in a process group of its own, so stopping one stops
/// everything it started too: `uv run` hands the work to a Python grandchild, and SIGTERM
/// to `uv` alone could leave that writing to a download's `.part` file after the app had
/// let it go. `Foundation.Process` cannot start a child in a group of its own, so this
/// spawns with `posix_spawn` (`POSIX_SPAWN_SETPGROUP`) and reaps with `waitpid` itself —
/// see `Child`.
///
/// Stopping (cancellation, a deadline, `LineStream.stop()`) is SIGTERM to the group, then
/// SIGKILL to whatever of it is still there `grace` later. A stopped run or stream ends only
/// once the child has been reaped *and* nothing is left in its group — a grandchild that
/// ignores SIGTERM keeps it waiting until SIGKILL has done its work — or, if something
/// outlives even that, with `RunnerError.stillRunning`.
public final class ProcessRunner: CommandRunner, Sendable {
    /// A test hook: handed each `run`'s collector, so a test can hold it weakly and see that
    /// it is freed once the run is over.
    private let observeCollector: (@Sendable (AnyObject) -> Void)?
    /// A test hook: handed each child as it is spawned, for what was done to it.
    private let observeChild: (@Sendable (Child) -> Void)?
    /// Between SIGTERM and SIGKILL.
    private let grace: Duration

    public init() {
        observeCollector = nil
        observeChild = nil
        grace = .seconds(3)
    }

    init(grace: Duration = .seconds(3), observingCollector: (@Sendable (AnyObject) -> Void)? = nil,
         observingChild: (@Sendable (Child) -> Void)? = nil) {
        self.grace = grace
        observeCollector = observingCollector
        observeChild = observingChild
    }

    public func run(_ request: CommandRequest) async throws -> CommandResult {
        let slot = ChildSlot()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<CommandResult, Error>) in
                let collector = RunCollector(continuation: continuation, grace: grace)
                observeCollector?(collector)

                let spawned: Spawned
                do {
                    spawned = try Child.spawn(request, stderr: .pipe, grace: grace)
                } catch {
                    collector.launchFailed(error)
                    return
                }

                observeChild?(spawned.child)
                collector.adopt(spawned.child)
                spawned.child.onExit { [weak collector] status in collector?.exited(status) }
                spawned.child.onGroupEnd { [weak collector] end in collector?.groupEnded(end) }
                collector.start(reading: [spawned.stdout, spawned.stderr].compactMap { $0 })
                if let timeout = request.timeout {
                    collector.arm(timeout)
                }
                // Cancelled already (before the spawn): stopped at once.
                slot.hold(spawned.child)
                spawned.writeStdin(request.stdin)
            }
        } onCancel: {
            slot.stop()
        }
    }

    public func lines(_ request: CommandRequest) -> LineStream {
        stream(request, interactive: false)
    }

    /// `lines`, with stdin a pipe the stream's `send` writes to. It is closed by
    /// `closeInput()`, by `stop()`, when the child exits, or when the consumer goes away —
    /// whichever comes first — and a line is written, never kept: once it is in the pipe
    /// nothing here holds it.
    public func interactiveLines(_ request: CommandRequest) -> LineStream {
        stream(request, interactive: true)
    }

    private func stream(_ request: CommandRequest, interactive: Bool) -> LineStream {
        let (stream, continuation) = AsyncThrowingStream<String, Error>.makeStream()
        let spawned: Spawned
        do {
            // stderr is discarded, but into `/dev/null`, not a pipe nobody reads: an unread
            // pipe's buffer fills (~64 KB) and blocks the child forever.
            spawned = try Child.spawn(request, stderr: .discard, grace: grace, keepInput: interactive)
        } catch {
            continuation.finish(throwing: error)
            return LineStream(stream)
        }
        let child = spawned.child
        observeChild?(child)
        let writer = interactive ? spawned.stdin.map(InputWriter.init(fd:)) : nil
        let reader = LineReader(continuation: continuation, child: child)
        if let stdout = spawned.stdout { reader.start(reading: stdout) }
        child.onExit { _ in
            writer?.close()
            reader.childExited()
        }
        child.onGroupEnd { end in reader.groupEnded(end) }
        continuation.onTermination = { termination in
            writer?.close()
            // The consumer went away (cancelled): stop the child, and stop reading. A stream
            // that finished by itself has nothing left to stop.
            if case .cancelled = termination {
                child.stop()
                reader.abandon()
            }
        }
        guard let writer else {
            spawned.writeStdin(request.stdin)
            return LineStream(stream, stop: { child.stop() })
        }
        if let data = request.stdin, !data.isEmpty { _ = writer.write(data) }
        let input = StreamInput(send: { writer.write(Data(($0 + "\n").utf8)) }, close: { writer.close() })
        return LineStream(stream, stop: {
            writer.close()
            child.stop()
        }, input: input)
    }
}

/// A child's stdin, kept open for lines written while it runs. Writes and the close go
/// through one serial queue, in order, so a line handed over before `close` is written
/// before the pipe closes; a write blocked on a child that is not reading ends when the
/// child exits (`EPIPE`, never SIGPIPE). Each line is held only until it is written.
///
/// A write is queued under the same lock that checks `closed`, and `close` queues the
/// close under it too, so the queue's order is the lock's: no write can land behind the
/// close and reach the descriptor's number after something else has been given it. The
/// queue's own `open`, which the close clears and every write checks, is the second
/// guard: a write that somehow arrives after it is dropped and counted (`lateWrites`).
final class InputWriter: @unchecked Sendable {
    private let fd: Int32
    private let queue = DispatchQueue(label: "dev.keryx.app.process-input")
    private let lock = NSLock()
    private var closed = false
    /// On `queue` only: the descriptor is still ours.
    private var open = true
    /// On `queue` only: writes that arrived after the close, and were dropped. None, by
    /// construction; for tests.
    private var dropped = 0

    init(fd: Int32) {
        self.fd = fd
        _ = fcntl(fd, F_SETNOSIGPIPE, 1)
    }

    /// Queues `data` for the pipe: false, and nothing queued, once it is closed.
    func write(_ data: Data) -> Bool {
        lock.withLock {
            guard !closed else { return false }
            queue.async { [self] in
                guard open else {
                    dropped += 1
                    return
                }
                Self.write(data, to: fd)
            }
            return true
        }
    }

    /// Closes the pipe, after whatever was queued before it. Only the first call does.
    func close() {
        lock.withLock {
            guard !closed else { return }
            closed = true
            queue.async { [self] in
                open = false
                _ = Darwin.close(fd)
            }
        }
    }

    /// Writes that reached the queue after its close (always 0), once every queued write
    /// and the close have run. For tests.
    var lateWrites: Int { queue.sync { dropped } }

    /// All of `data`, unless the pipe fails (`EPIPE` from a child that is gone): then the
    /// rest is dropped.
    private static func write(_ data: Data, to fd: Int32) {
        data.withUnsafeBytes { buffer in
            guard var base = buffer.baseAddress else { return }
            var left = buffer.count
            while left > 0 {
                let written = Darwin.write(fd, base, left)
                if written < 0 {
                    if errno == EINTR { continue }
                    return
                }
                left -= written
                base += written
            }
        }
    }
}

// MARK: - The child

/// One spawned child: its pid (also its process group's id), its exit, and how to stop it.
///
/// A thread of its own waits for the child to exit (`waitid` with `WNOWAIT`) without
/// reaping it: an unreaped leader, a zombie, holds its pid and so the group's id, and the
/// group may only be signalled while that is so — once the leader is reaped the number may
/// be free, and a signal to it could reach an unrelated group that took it. So the leader is
/// reaped exactly once, last, and the group is closed (never signalled again) first:
///
/// - by its owner's `release()`, when the run or stream is over and nobody stopped it;
/// - by `stop()` itself, which signals and polls the group (`proc_listpgrppids`, counting
///   every member but the leader) until the leader has exited and nothing else is left, or
///   the SIGKILL deadline has passed.
///
/// Handlers run on the waiting thread or the polling queue.
final class Child: @unchecked Sendable {
    enum Output { case pipe, discard }

    /// How a stopped child's process group ended: emptied, or still there after SIGKILL and
    /// `killWait` more.
    enum GroupEnd: Sendable, Equatable { case gone, survived }

    /// What was done to the child, in order: for tests.
    enum Event: Sendable, Equatable { case signalled(Int32), reaped }

    /// How long after SIGKILL the group has to be gone before it is reported as surviving.
    static let killWait: Duration = .seconds(1)
    /// How often the group is asked whether anything is left in it.
    static let pollEvery: Duration = .milliseconds(20)

    let pid: pid_t
    private let grace: Duration
    private let lock = NSLock()
    private var status: Int32?
    private var handlers: [@Sendable (Int32) -> Void] = []
    private var stopRequested = false
    private var groupEnd: GroupEnd?
    private var groupHandlers: [@Sendable (GroupEnd) -> Void] = []
    /// The leader has exited (it may still be an unreaped zombie).
    private var leaderExited = false
    /// The group will never be signalled again: set before the leader is reaped.
    private var closed = false
    /// Someone has taken on reaping the leader: it is reaped exactly once.
    private var reapClaimed = false
    private var log: [Event] = []

    private init(pid: pid_t, grace: Duration) {
        self.pid = pid
        self.grace = grace
    }

    /// True once `stop()` has been called.
    var isStopping: Bool { lock.withLock { stopRequested } }

    /// What was done to the child so far.
    var events: [Event] { lock.withLock { log } }

    /// Calls `handler` with the exit status — the exit code, or the signal that ended it, as
    /// `Process.terminationStatus` has it — once the child has exited; at once if it has.
    func onExit(_ handler: @escaping @Sendable (Int32) -> Void) {
        let known: Int32? = lock.withLock {
            if status == nil { handlers.append(handler) }
            return status
        }
        if let known { handler(known) }
    }

    /// Calls `handler` once a stopped child's whole process group has ended (see `stop`); at
    /// once if it has. Never called for a child nobody stopped.
    func onGroupEnd(_ handler: @escaping @Sendable (GroupEnd) -> Void) {
        let known: GroupEnd? = lock.withLock {
            if groupEnd == nil { groupHandlers.append(handler) }
            return groupEnd
        }
        if let known { handler(known) }
    }

    /// SIGTERM to the whole process group, then a look every `pollEvery`: done once the
    /// leader has exited and nothing else is in the group. Still there after `grace`:
    /// SIGKILL, and `killWait` more before it is reported as `survived`. A child that already
    /// ended by itself (reaped, the group closed) is not signalled: its group is `gone` as
    /// far as this child is concerned. Only the first call does anything.
    func stop() {
        let signalled: Bool? = lock.withLock {
            guard !stopRequested else { return nil }
            stopRequested = true
            guard !closed else { return false }
            signal(SIGTERM)
            return true
        }
        switch signalled {
        case nil: return
        case false?: groupEnded(.gone)
        case true?: poll(termDeadline: .now() + grace.timeInterval)
        }
    }

    /// The thread's part: the leader has exited with `value`. Reaped now only if its group
    /// was already closed (released before the exit, or a stop that gave up on it).
    fileprivate func leaderDidExit(_ value: Int32) {
        let (reapNow, waiting): (Bool, [@Sendable (Int32) -> Void]) = lock.withLock {
            leaderExited = true
            status = value
            defer { handlers = [] }
            return (closed && claimReap(), handlers)
        }
        waiting.forEach { $0(value) }
        if reapNow { reap() }
    }

    /// The run or stream is over: the group is closed for good — nothing of it is signalled
    /// again, whatever it left behind — and the leader reaped (now, or when it exits). Does
    /// nothing while a stop is under way, which ends the same way by itself.
    func release() {
        let reapNow: Bool = lock.withLock {
            guard !closed, !stopRequested else { return false }
            closed = true
            return leaderExited && claimReap()
        }
        if reapNow { reap() }
    }

    /// One look at a stopping group.
    private func poll(termDeadline: DispatchTime, killDeadline: DispatchTime? = nil) {
        var killDeadline = killDeadline
        var end: GroupEnd?
        let reapNow: Bool = lock.withLock {
            guard !closed else { return false }
            let now = DispatchTime.now()
            if leaderExited && Self.othersInGroup(pid) == 0 {
                end = .gone
            } else if let deadline = killDeadline {
                if now >= deadline { end = .survived }
            } else if now >= termDeadline {
                signal(SIGKILL)
                killDeadline = now + Self.killWait.timeInterval
            }
            guard end != nil else { return false }
            closed = true
            return leaderExited && claimReap()
        }
        if reapNow { reap() }
        if let end { return groupEnded(end) }
        let next = killDeadline
        DispatchQueue.global().asyncAfter(deadline: .now() + Self.pollEvery.timeInterval) { [self] in
            poll(termDeadline: termDeadline, killDeadline: next)
        }
    }

    /// Signals the group. Only under `lock`, and only while it is open — the leader
    /// unreaped, so its pid and the group id are still ours.
    private func signal(_ number: Int32) {
        precondition(!closed, "signalling a closed process group")
        kill(-pid, number)
        log.append(.signalled(number))
    }

    /// Under `lock`: true the first time only.
    private func claimReap() -> Bool {
        defer { reapClaimed = true }
        return !reapClaimed
    }

    /// How many processes other than `leader` are in its group.
    static func othersInGroup(_ leader: pid_t) -> Int {
        var members = [pid_t](repeating: 0, count: 256)
        let count = members.withUnsafeMutableBytes {
            proc_listpgrppids(leader, $0.baseAddress, Int32($0.count))
        }
        guard count > 0 else { return 0 }
        return members.prefix(min(Int(count), members.count)).filter { $0 != 0 && $0 != leader }.count
    }

    /// Reaps the leader, which has exited. Called once (`claimReap`), after `closed`.
    private func reap() {
        var status: Int32 = 0
        var waited: pid_t
        repeat { waited = waitpid(pid, &status, 0) } while waited == -1 && errno == EINTR
        lock.withLock { log.append(.reaped) }
    }

    private func groupEnded(_ end: GroupEnd) {
        let waiting: [@Sendable (GroupEnd) -> Void] = lock.withLock {
            groupEnd = end
            defer { groupHandlers = [] }
            return groupHandlers
        }
        waiting.forEach { $0(end) }
    }

    /// Starts `request` in a new process group: stdout to a pipe; stderr to a pipe or to
    /// `/dev/null`; stdin from a pipe when there is input to give (or `keepInput`), else
    /// `/dev/null`; the
    /// environment exactly as given (an empty one is empty, not the parent's); in the
    /// working directory asked for; no other descriptor inherited; the signal mask and every
    /// signal's disposition back to the defaults.
    /// `keepInput`: stdin is a pipe even with no `request.stdin`, for the caller to write
    /// to and close (`InputWriter`).
    static func spawn(_ request: CommandRequest, stderr: Output, grace: Duration,
                      keepInput: Bool = false) throws -> Spawned {
        var opened: [Int32] = []
        func pipePair() throws -> (read: Int32, write: Int32) {
            var fds: [Int32] = [-1, -1]
            guard pipe(&fds) == 0 else { throw RunnerError.launchFailed(String(cString: strerror(errno))) }
            for fd in fds { _ = fcntl(fd, F_SETFD, FD_CLOEXEC) }
            opened += fds
            return (fds[0], fds[1])
        }

        let out: (read: Int32, write: Int32)
        var err: (read: Int32, write: Int32)?
        var input: (read: Int32, write: Int32)?
        do {
            out = try pipePair()
            if stderr == .pipe { err = try pipePair() }
            if request.stdin != nil || keepInput { input = try pipePair() }
        } catch {
            opened.forEach { close($0) }
            throw error
        }

        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        if let input {
            posix_spawn_file_actions_adddup2(&actions, input.read, 0)
        } else {
            posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0)
        }
        posix_spawn_file_actions_adddup2(&actions, out.write, 1)
        if let err {
            posix_spawn_file_actions_adddup2(&actions, err.write, 2)
        } else {
            posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0)
        }
        if let directory = request.workingDirectory {
            posix_spawn_file_actions_addchdir_np(&actions, directory)
        }

        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }
        posix_spawnattr_setflags(&attributes, Int16(
            POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT
                | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK))
        posix_spawnattr_setpgroup(&attributes, 0) // a group of its own: pgid = its pid
        var defaults = sigset_t()
        sigfillset(&defaults)
        sigdelset(&defaults, SIGKILL)
        sigdelset(&defaults, SIGSTOP)
        posix_spawnattr_setsigdefault(&attributes, &defaults)
        var mask = sigset_t()
        sigemptyset(&mask)
        posix_spawnattr_setsigmask(&attributes, &mask)

        let argv: [UnsafeMutablePointer<CChar>?] =
            ([request.executable] + request.arguments).map { strdup($0) } + [nil]
        let envp: [UnsafeMutablePointer<CChar>?] =
            request.environment.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer {
            argv.forEach { free($0) }
            envp.forEach { free($0) }
        }

        var pid = pid_t()
        let result = posix_spawn(&pid, request.executable, &actions, &attributes, argv, envp)
        // The child's ends are the child's now (or, on failure, nobody's).
        close(out.write)
        if let err { close(err.write) }
        if let input { close(input.read) }
        guard result == 0 else {
            close(out.read)
            if let err { close(err.read) }
            if let input { close(input.write) }
            throw RunnerError.launchFailed("\(request.executable): \(String(cString: strerror(result)))")
        }

        let spawnedPID = pid
        let child = Child(pid: spawnedPID, grace: grace)
        let waiter = Thread {
            // Exited, but not reaped (`WNOWAIT`): `Child` decides when.
            var info = siginfo_t()
            var result: Int32
            repeat {
                result = waitid(P_PID, id_t(spawnedPID), &info, WEXITED | WNOWAIT)
            } while result == -1 && errno == EINTR
            child.leaderDidExit(result == 0 ? Child.terminationStatus(info) : -1)
        }
        waiter.name = "dev.keryx.app.wait"
        waiter.stackSize = 64 * 1024
        waiter.start()
        return Spawned(child: child, stdout: out.read, stderr: err?.read, stdin: input?.write)
    }

    /// An exit as `Process.terminationStatus` reports it: the exit code, or the number of
    /// the signal that ended the child.
    static func terminationStatus(_ info: siginfo_t) -> Int32 {
        info.si_status
    }
}

/// A child just spawned, and the parent's ends of its pipes.
struct Spawned {
    let child: Child
    let stdout: Int32?
    let stderr: Int32?
    let stdin: Int32?

    /// Writes `data` to the child's stdin and closes it. A child that exits without reading
    /// it is no reason for a SIGPIPE here (`F_SETNOSIGPIPE`): what it did not read is dropped.
    func writeStdin(_ data: Data?) {
        guard let fd = stdin else { return }
        defer { close(fd) }
        guard let data, !data.isEmpty else { return }
        _ = fcntl(fd, F_SETNOSIGPIPE, 1)
        data.withUnsafeBytes { buffer in
            guard var base = buffer.baseAddress else { return }
            var left = buffer.count
            while left > 0 {
                let written = Darwin.write(fd, base, left)
                if written < 0 {
                    if errno == EINTR { continue }
                    return
                }
                left -= written
                base += written
            }
        }
    }
}

/// The child of a `run`, for its cancellation handler — which may run before the child
/// exists (a task cancelled before it started), and then stops it the moment it does.
private final class ChildSlot: @unchecked Sendable {
    private let lock = NSLock()
    private var child: Child?
    private var cancelled = false

    func hold(_ child: Child) {
        let stopNow = lock.withLock {
            self.child = child
            return cancelled
        }
        if stopNow { child.stop() }
    }

    func stop() {
        let child: Child? = lock.withLock {
            cancelled = true
            return self.child
        }
        child?.stop()
    }
}

// MARK: - Collecting a run

/// One `run`: the two output streams, the exit, and the timers, all confined to one serial
/// queue. The deadline bounds the whole wait, not only the child's life: a grandchild the
/// child left in the background (`cmd &` in a shell's rc files) can hold stdout or stderr
/// open long after the child has exited, and a reader blocked until EOF would wait for it.
/// So the streams are read by dispatch read sources rather than blocking reads, and on
/// expiry the child's group is stopped, the run waits for the child to be reaped (SIGKILL
/// bounds that), and `.timedOut` is thrown.
///
/// A stopped child (cancelled, or past the deadline) ends the run once it has been reaped
/// and its whole group is gone, with what it said by then — its output has `drain` more to
/// reach EOF, not for ever — or, when the group outlived SIGKILL, with `.stillRunning`.
///
/// Lifetime: the collector holds itself (`running`) from creation until it concludes, and
/// nothing else holds it strongly — the exit handler, the timers and the sources' handlers
/// all capture it weakly. Every terminal path (finish, expiry, launch failure) goes through
/// `conclude`, which resumes the continuation exactly once and drops the self-reference, the
/// timers and the sources, so a run never outlives its result.
private final class RunCollector: @unchecked Sendable {
    /// How long a stopped child's output has, once it was reaped, to reach EOF.
    static let drain: Duration = .milliseconds(500)

    private let queue = DispatchQueue(label: "dev.keryx.app.process-runner")
    private let grace: Duration
    private var child: Child?
    private var continuation: CheckedContinuation<CommandResult, Error>?
    private var running: RunCollector?
    private var sources: [DispatchSourceRead] = []
    private var output = [Data(), Data()]
    /// Streams not yet at EOF. Two from the start, so an exit reported before `start`
    /// cannot finish the run with nothing read.
    private var open = 2
    private var exitCode: Int32?
    private var timedOut = false
    private var groupEnd: Child.GroupEnd?
    private var draining = false
    private var timers: [DispatchWorkItem] = []
    private static let chunk = 65_536
    private var buffer = [UInt8](repeating: 0, count: RunCollector.chunk)

    init(continuation: CheckedContinuation<CommandResult, Error>, grace: Duration) {
        self.continuation = continuation
        self.grace = grace
        running = self
    }

    func adopt(_ child: Child) {
        queue.sync { self.child = child }
    }

    /// `descriptors[0]` is stdout and `descriptors[1]` stderr. Each is closed by its
    /// source's cancel handler, which runs once, after the source's last event.
    func start(reading descriptors: [Int32]) {
        queue.sync {
            open = descriptors.count
            for (index, descriptor) in descriptors.enumerated() {
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
                    // Closed whether or not the run is still waiting: the descriptor is the
                    // source's to close, once its last event has run.
                    close(descriptor)
                    guard let self else { return }
                    open -= 1
                    evaluate()
                }
                sources.append(source)
                source.resume()
            }
        }
    }

    func arm(_ timeout: Duration) {
        queue.sync {
            // Already concluded (a run quicker than this call): nothing to arm.
            guard continuation != nil else { return }
            schedule(after: timeout) { $0.expire() }
        }
    }

    func exited(_ code: Int32) {
        queue.async { [weak self] in
            guard let self else { return }
            exitCode = code
            evaluate()
        }
    }

    func groupEnded(_ end: Child.GroupEnd) {
        queue.async { [weak self] in
            guard let self else { return }
            groupEnd = end
            evaluate()
        }
    }

    func launchFailed(_ error: Error) {
        queue.sync {
            conclude(.failure(error as? RunnerError ?? RunnerError.launchFailed("\(error)")))
        }
    }

    // MARK: On `queue`

    private func schedule(after delay: Duration, _ action: @escaping (RunCollector) -> Void) {
        let item = DispatchWorkItem { [weak self] in
            guard let self else { return }
            action(self)
        }
        timers.append(item)
        queue.asyncAfter(deadline: .now() + delay.timeInterval, execute: item)
    }

    /// Whether the run is over, and how. A child nobody stopped is done once it has exited
    /// and its output is at EOF. A stopped one waits for its group to end as well.
    private func evaluate() {
        guard continuation != nil else { return }
        if groupEnd == .survived {
            return conclude(.failure(RunnerError.stillRunning))
        }
        guard exitCode != nil else { return }
        guard child?.isStopping == true else {
            if open == 0 { concludeWithOutput() }
            return
        }
        guard groupEnd == .gone else { return }
        if timedOut { return conclude(.failure(RunnerError.timedOut)) }
        if open == 0 {
            concludeWithOutput()
        } else if !draining {
            draining = true
            schedule(after: Self.drain) { $0.concludeWithOutput() }
        }
    }

    private func concludeWithOutput() {
        guard let exitCode else { return }
        conclude(.success(CommandResult(exitCode: exitCode, stdout: output[0], stderr: output[1])))
    }

    /// Past the deadline: the group is stopped, and the run ends as `.timedOut` once the
    /// child has been reaped and its group is gone (`evaluate`) — bounded by SIGKILL and
    /// `Child.killWait`, after which a group still there is `.stillRunning`.
    private func expire() {
        guard continuation != nil else { return }
        timedOut = true
        child?.stop()
        evaluate()
    }

    /// The one way out. Cancelling a source that has already finished does nothing; one
    /// still open is cancelled, and its cancel handler closes its descriptor.
    private func conclude(_ result: Result<CommandResult, Error>) {
        guard let continuation else { return }
        self.continuation = nil
        child?.release()
        timers.forEach { $0.cancel() }
        timers = []
        sources.forEach { $0.cancel() }
        sources = []
        running = nil
        continuation.resume(with: result)
    }
}

// MARK: - Reading lines

/// The stdout of one `lines` child, split into lines on one serial queue. The stream
/// finishes once stdout is at EOF and the child has been reaped — or, when something the
/// child left behind holds stdout open, `drain` after it was reaped — and, for a child that
/// was stopped, once its whole group is gone. A group that outlived SIGKILL ends the stream
/// with `RunnerError.stillRunning`.
private final class LineReader: @unchecked Sendable {
    static let drain: Duration = .seconds(1)

    private let queue = DispatchQueue(label: "dev.keryx.app.process-lines")
    private let continuation: AsyncThrowingStream<String, Error>.Continuation
    private let child: Child
    private var source: DispatchSourceRead?
    private var pending = Data()
    private var atEnd = false
    private var exited = false
    private var groupEnd: Child.GroupEnd?
    private var finished = false
    private static let chunk = 65_536
    private var buffer = [UInt8](repeating: 0, count: LineReader.chunk)

    init(continuation: AsyncThrowingStream<String, Error>.Continuation, child: Child) {
        self.continuation = continuation
        self.child = child
    }

    /// The source holds the reader (through its handlers) until it is cancelled; its cancel
    /// handler closes the descriptor and lets go of it.
    func start(reading descriptor: Int32) {
        queue.sync {
            let source = DispatchSource.makeReadSource(fileDescriptor: descriptor, queue: queue)
            source.setEventHandler { [self] in
                let count = Darwin.read(descriptor, &buffer, Self.chunk)
                if count > 0 {
                    pending.append(contentsOf: buffer[0..<count])
                    yieldCompleteLines()
                } else if count == 0 || (errno != EINTR && errno != EAGAIN) {
                    self.source?.cancel()
                }
            }
            source.setCancelHandler { [self] in
                close(descriptor)
                self.source = nil
                atEnd = true
                if !pending.isEmpty {
                    yield(pending)
                    pending = Data()
                }
                finishIfDone()
            }
            self.source = source
            source.resume()
        }
    }

    func childExited() {
        queue.async { [self] in
            exited = true
            if !atEnd {
                queue.asyncAfter(deadline: .now() + Self.drain.timeInterval) { [self] in
                    source?.cancel()
                }
            }
            finishIfDone()
        }
    }

    func groupEnded(_ end: Child.GroupEnd) {
        queue.async { [self] in
            groupEnd = end
            finishIfDone()
        }
    }

    /// The consumer has gone: stop reading.
    func abandon() {
        queue.async { [self] in source?.cancel() }
    }

    // MARK: On `queue`

    private func yieldCompleteLines() {
        while let newline = pending.firstIndex(of: UInt8(ascii: "\n")) {
            yield(pending[pending.startIndex..<newline])
            pending = Data(pending[pending.index(after: newline)...])
        }
    }

    private func yield(_ bytes: Data) {
        var line = String(decoding: bytes, as: UTF8.self)
        if line.hasSuffix("\r") { line.removeLast() }
        continuation.yield(line)
    }

    private func finishIfDone() {
        guard !finished else { return }
        if groupEnd == .survived {
            finished = true
            source?.cancel()
            return continuation.finish(throwing: RunnerError.stillRunning)
        }
        guard atEnd, exited, !child.isStopping || groupEnd == .gone else { return }
        finished = true
        child.release()
        continuation.finish()
    }
}

extension Duration {
    var timeInterval: TimeInterval {
        let (seconds, attoseconds) = (components.seconds, components.attoseconds)
        return TimeInterval(seconds) + TimeInterval(attoseconds) / 1e18
    }
}
