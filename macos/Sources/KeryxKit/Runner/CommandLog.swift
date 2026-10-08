import Foundation
import os

/// What may be said about a command in the log: its subcommand, its verb and its flags,
/// from fixed lists. Everything else — a setting's key or value, a model name, a task id, a
/// restart reason, a shell script, a path — is `…`.
///
/// An allowlist, so a value can only reach the log by being one of these words: a secret
/// never could (it reaches the CLI on stdin), and neither can a phone number, a name or
/// anything else the owner typed.
public enum CommandLog {
    /// `keryx` subcommands, each with the verbs it may be followed by.
    static let subcommands: [String: Set<String>] = [
        "status": [], "doctor": [], "approvals": [], "memory": [], "restart": [],
        "tasks": ["list", "show", "usage"],
        "models": ["list", "pull", "use", "start", "stop"],
        "config": ["list", "set", "unset", "options", "verify"],
        "voice": ["use", "download"],
        "setup": ["plan", "mark", "profile", "project-context"],
        "pin": ["set"],
        "phone": ["numbers", "webhooks", "tunnel"],
        "service": ["install", "uninstall"],
        "auth": ["login", "logout", "smoke"],
        "integrations": [],
        "plugins": ["list", "install", "remove", "set", "hosts"],
        "tools": [], "migrate": [],
    ]

    /// The words a subcommand and verb may be followed by (`keryx voice use here`): fixed
    /// names, never something the owner typed. A setup step's id is not one of them (it is
    /// a value), nor is a plugin's setting.
    static let modes: [String: Set<String>] = [
        "voice use": ["openai", "server", "here"],
        "auth login": ["claude", "codex", "gmail", "google-workspace"],
        "auth logout": ["gmail", "google-workspace"],
        "auth smoke": ["claude", "codex", "local"],
        "plugins install": plugins,
        "plugins remove": plugins,
        "plugins set": plugins,
    ]

    /// The plugins, by name: Keryx's own four.
    static let plugins: Set<String> = ["send_to_slack", "check_email", "check_billing", "cluster_stats"]

    static let flags: Set<String> = [
        "--json", "--stdin", "--force", "--internal", "--limit", "--days", "--status",
        "--default-agent", "--disable", "--enable", "--llm", "--key-stdin", "--clear-key", "--keep-anyway",
        "--voice", "--pair", "--replace", "--apply", "--yes", "--preview", "--describe", "--explore",
    ]

    /// The flags that take a value: the flag is logged, the value never — `--set=…` when
    /// written as one word, `--set …` as two.
    static let valueFlags: Set<String> = [
        "--set", "--cluster", "--guard", "--client-file", "--runtime", "--folder",
    ]

    static let elided = "…"

    /// `keryx config set … --stdin`, for a command run as `executable arguments`, the first
    /// `prefixCount` of which are the way to run `keryx` (`run --project … keryx`) rather
    /// than the command. With a prefix, or an executable named `keryx`, the program is
    /// `keryx` and its subcommands may be named; any other program is named by its file
    /// name, with only the listed flags.
    public static func describe(executable: String, arguments: [String], prefixCount: Int = 0) -> String {
        let file = (executable as NSString).lastPathComponent
        let isKeryx = prefixCount > 0 || file == "keryx"
        let command = Array(arguments.dropFirst(prefixCount))
        var words = [isKeryx ? "keryx" : file]
        var verbs: Set<String>?
        var named: Set<String>?
        for (index, argument) in command.enumerated() {
            if argument == "--" {
                // Everything after `--` is a value, whatever it looks like.
                words.append(elided)
                break
            }
            if index == 0, isKeryx, let allowed = subcommands[argument] {
                words.append(argument)
                verbs = allowed
            } else if index == 1, let allowed = verbs, allowed.contains(argument) {
                words.append(argument)
                named = modes[command[0] + " " + argument]
            } else if index == 2, let allowed = named, allowed.contains(argument) {
                words.append(argument)
            } else if flags.contains(argument) || valueFlags.contains(argument) {
                words.append(argument)
            } else if let flag = argument.split(separator: "=", maxSplits: 1).first.map(String.init),
                      argument.contains("="), valueFlags.contains(flag) {
                words.append(flag + "=" + elided)
            } else {
                words.append(elided)
            }
        }
        return collapse(words).joined(separator: " ")
    }

    /// The name of an error's case, for the log: never its message, which may hold a path
    /// (and so the user's name) or the command's own output.
    public static func errorName(_ error: Error) -> String {
        switch error {
        case let runner as RunnerError:
            switch runner {
            case .launchFailed: "launchFailed"
            case .timedOut: "timedOut"
            case .stillRunning: "stillRunning"
            }
        case is CancellationError: "cancelled"
        default: String(describing: type(of: error))
        }
    }

    /// Runs of `…` as one.
    private static func collapse(_ words: [String]) -> [String] {
        var result: [String] = []
        for word in words where !(word == elided && result.last == elided) {
            result.append(word)
        }
        return result
    }
}

/// A `CommandRunner` that writes one line per command to the unified log (subsystem
/// `dev.keryx.app`, category `commands`): `CommandLog.describe`, and how it ended. stdin,
/// the environment and the output are never logged.
public struct LoggingRunner: CommandRunner {
    static let logger = Logger(subsystem: "dev.keryx.app", category: "commands")

    private let base: CommandRunner
    private let prefix: [String]

    /// `prefix`: the leading arguments that run `keryx` rather than name the command
    /// (`run --project … keryx` for uv). A request is read as `keryx` past them only when it
    /// starts with them: anything else run through it (the locator's login shell) is
    /// that program, named by its file.
    public init(_ base: CommandRunner = ProcessRunner(), prefix: [String] = []) {
        self.base = base
        self.prefix = prefix
    }

    public func run(_ request: CommandRequest) async throws -> CommandResult {
        let command = describe(request)
        let clock = ContinuousClock()
        let start = clock.now
        do {
            let result = try await base.run(request)
            let seconds = Self.seconds(clock.now - start)
            if result.exitCode == 0 {
                Self.logger.info("\(command, privacy: .public) exited 0 in \(seconds, privacy: .public)")
            } else {
                Self.logger.notice(
                    "\(command, privacy: .public) exited \(result.exitCode, privacy: .public) in \(seconds, privacy: .public)")
            }
            return result
        } catch {
            Self.logger.error(
                "\(command, privacy: .public) did not finish: \(CommandLog.errorName(error), privacy: .public)")
            throw error
        }
    }

    public func lines(_ request: CommandRequest) -> LineStream {
        Self.logger.info("\(describe(request), privacy: .public) started streaming")
        return base.lines(request)
    }

    /// Logged as `lines` is; what is sent on the stream's input never is.
    public func interactiveLines(_ request: CommandRequest) -> LineStream {
        Self.logger.info("\(describe(request), privacy: .public) started streaming, taking input")
        return base.interactiveLines(request)
    }

    func describe(_ request: CommandRequest) -> String {
        let prefixed = !prefix.isEmpty && request.arguments.starts(with: prefix)
        return CommandLog.describe(executable: request.executable, arguments: request.arguments,
                                   prefixCount: prefixed ? prefix.count : 0)
    }

    private static func seconds(_ duration: Duration) -> String {
        let (whole, attoseconds) = duration.components
        return String(format: "%.2f s", Double(whole) + Double(attoseconds) / 1e18)
    }
}
