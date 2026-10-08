import Foundation
import Observation

/// Installing Cloudflare Tunnel with Homebrew (`brew install cloudflared`), one install at a
/// time, its output read line by line. Homebrew says nothing a person needs to read, so a
/// line is only ever the detail under the run's own sentence. Whether it worked is asked of
/// Keryx afterwards (`check`): the tunnel program found on the PATH, or not.
///
/// `cancel` stops Homebrew's whole process group, and the phase reads `.cancelling` until
/// it has gone; a cancelled install leaves no phase.
@Observable @MainActor
public final class ProgramInstall {
    public enum Phase: Equatable, Sendable {
        /// `line`: Homebrew's last line, for Show Details.
        case running(line: String?)
        case cancelling
        case failed(StreamFailure)
        case finished
    }

    public private(set) var phase: Phase?
    @ObservationIgnored private var stop: (@Sendable () -> Void)?
    @ObservationIgnored private var stopping = false
    @ObservationIgnored private var run: UUID?
    @ObservationIgnored private var task: Task<Void, Never>?

    public init() {}

    public var isRunning: Bool {
        if case .running = phase { true } else { false }
    }

    public var isBusy: Bool { isRunning || phase == .cancelling }

    /// Starts the install unless one is going. `check` says, once Homebrew has ended,
    /// whether the program is now there.
    public func start(_ stream: StoppableStream<String>, check: @escaping @MainActor () async -> Bool) {
        guard !isBusy else { return }
        let run = UUID()
        self.run = run
        stopping = false
        phase = .running(line: nil)
        stop = { stream.stop() }
        task = Task { [weak self] in
            var lines: [String] = []
            var thrown: Error?
            do {
                for try await line in stream {
                    lines = Array((lines + [line]).suffix(12))
                    guard let self, self.run == run, !self.stopping else { continue }
                    self.phase = .running(line: line)
                }
            } catch {
                thrown = error
            }
            guard let self, self.run == run else { return }
            if stopping {
                phase = nil
                return
            }
            let there = await check()
            let detail = ([thrown.map { ($0 as? KeryxError)?.message ?? $0.localizedDescription }].compactMap { $0 }
                + lines).joined(separator: "\n")
            phase = there ? .finished : .failed(StreamFailure(Self.didNotInstall, detail: detail))
        }
    }

    public func cancel() {
        guard isRunning else { return }
        stopping = true
        phase = .cancelling
        stop?()
    }

    public func dismiss() {
        guard !isBusy else { return }
        phase = nil
    }

    /// Waits for the install under way to end. For tests.
    func settled() async {
        await task?.value
    }

    public static let didNotInstall = "Cloudflare Tunnel didn't install."
}
