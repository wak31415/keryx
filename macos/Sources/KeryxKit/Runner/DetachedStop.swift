import Darwin
import Foundation

/// Quit's `keryx service stop --all`, which runs detached and can outlive the app (its units
/// take up to a minute each to stop): the process, by its pid and the moment it started, so
/// the next launch can tell it is still going — and wait for it before starting Keryx again,
/// rather than run `service start --all` alongside it — and never mistake a later process
/// that was given the same pid for it.
public struct DetachedStop: Sendable, Equatable, Codable {
    public var pid: Int32
    /// When the process started, as the kernel says (seconds and microseconds since 1970).
    public var started: UInt64

    public init(pid: Int32, started: UInt64) {
        self.pid = pid
        self.started = started
    }

    /// The process `pid` is now, or `nil` when there is none.
    public static func of(pid: Int32) -> DetachedStop? {
        startTime(pid).map { DetachedStop(pid: pid, started: $0) }
    }

    /// Whether this very process is still running (not merely its pid).
    public var isRunning: Bool {
        Self.startTime(pid) == started
    }

    /// `pid`'s start time, from the kernel; `nil` when no process has that pid.
    static func startTime(_ pid: Int32) -> UInt64? {
        guard pid > 0 else { return nil }
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
        // A zombie still has its entry; it has finished all the same.
        if info.pbi_status == UInt32(SZOMB) { return nil }
        return info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec
    }
}
