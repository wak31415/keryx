import Foundation
import KeryxKit
import os

/// The app's unified-log channels (`log show --predicate 'subsystem == "dev.keryx.app"'`).
enum Log {
    static let subsystem = "dev.keryx.app"
    static let app = Logger(subsystem: subsystem, category: "app")
    static let commands = Logger(subsystem: subsystem, category: "commands")
}

/// A `CommandRunner` that writes one log line per command: what ran and how it ended.
///
/// Only the command's leading words reach the log — the subcommand and its flags, up to the
/// first word that is a value (a setting's key or value, a model name, a path). So a setting
/// written with `config set` is logged as `config set …`, and a secret is never there to
/// log in the first place: it reaches the CLI on stdin, which is never logged, as are the
/// environment and the output.
struct LoggingRunner: CommandRunner {
    private let base: CommandRunner
    /// How many leading arguments are the way to run `keryx` (`run --project … keryx`),
    /// not the command.
    private let prefixCount: Int

    init(_ base: CommandRunner = ProcessRunner(), prefixCount: Int = 0) {
        self.base = base
        self.prefixCount = prefixCount
    }

    func run(_ request: CommandRequest) async throws -> CommandResult {
        let command = describe(request)
        let clock = ContinuousClock()
        let start = clock.now
        do {
            let result = try await base.run(request)
            let seconds = Self.seconds(clock.now - start)
            if result.exitCode == 0 {
                Log.commands.info("\(command, privacy: .public) exited 0 in \(seconds, privacy: .public)")
            } else {
                Log.commands.notice(
                    "\(command, privacy: .public) exited \(result.exitCode, privacy: .public) in \(seconds, privacy: .public)")
            }
            return result
        } catch {
            Log.commands.error(
                "\(command, privacy: .public) did not run: \(String(describing: error), privacy: .public)")
            throw error
        }
    }

    func lines(_ request: CommandRequest) -> AsyncThrowingStream<String, Error> {
        Log.commands.info("\(describe(request), privacy: .public) started streaming")
        return base.lines(request)
    }

    private func describe(_ request: CommandRequest) -> String {
        let program = (request.executable as NSString).lastPathComponent
        let name = prefixCount > 0 ? "keryx" : program
        return Self.describe(name: name, arguments: Array(request.arguments.dropFirst(prefixCount)))
    }

    /// `name` and the leading arguments that are words or flags (`tasks list --json`), with
    /// `…` standing for whatever follows.
    static func describe(name: String, arguments: [String]) -> String {
        let words = arguments.prefix { isWord($0) }
        let rest = words.count < arguments.count ? ["…"] : []
        return ([name] + words + rest).joined(separator: " ")
    }

    private static func isWord(_ argument: String) -> Bool {
        let body = argument.drop { $0 == "-" }
        guard argument.count - body.count <= 2, let first = body.first, first.isLetter, first.isLowercase
        else { return false }
        return body.allSatisfy { ($0.isLetter && $0.isLowercase) || $0 == "-" }
    }

    private static func seconds(_ duration: Duration) -> String {
        let (whole, attoseconds) = duration.components
        let value = Double(whole) + Double(attoseconds) / 1e18
        return String(format: "%.2f s", value)
    }
}
