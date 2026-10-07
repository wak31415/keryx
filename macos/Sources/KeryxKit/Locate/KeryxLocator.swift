import Foundation

/// Finds the `keryx` CLI the way its launchd service runs it. A GUI app inherits no shell
/// environment, and the service runs with its installer's `KERYX_HOME` and XDG homes, so a
/// bare `keryx` on some PATH would read different settings from the service's. In order:
///
/// 1. The owner's chosen folder (`preference`): a checkout, when its `pyproject.toml` names
///    `keryx`.
/// 2. `~/Library/LaunchAgents/dev.keryx.agent.plist`: the service's own program, folder and
///    environment.
/// 3. The login shell: its environment, and `command -v keryx`; then `~/.local/bin/keryx`.
///
/// A checkout is optional: a `keryx` installed on its own runs with no prefix from `HOME`.
/// Every step that does not do appends one sentence to `LocateFailure.lookedAt`.
public struct KeryxLocator: Locating {
    static let agentPlist = "Library/LaunchAgents/dev.keryx.agent.plist"

    private let fileSystem: FileSystem
    private let runner: CommandRunner
    private let home: String

    public init(fileSystem: FileSystem = LocalFileSystem(), runner: CommandRunner = ProcessRunner(),
                home: String = NSHomeDirectory()) {
        self.fileSystem = fileSystem
        self.runner = runner
        self.home = home
    }

    public func locate(preference: String?) async -> Result<KeryxLocation, LocateFailure> {
        var lookedAt: [String] = []
        var shell = LoginShell()

        if let preference {
            if isKeryxCheckout(preference) {
                let environment = await shell.environment(runner: runner, base: processEnvironment)
                if let location = forCheckout(
                    preference, uv: nil, environment: environment ?? processEnvironment,
                    source: .preference, lookedAt: &lookedAt) {
                    return .success(location)
                }
            } else {
                lookedAt.append("The folder you chose, \(tilde(preference)), has no pyproject.toml naming keryx.")
            }
        } else {
            lookedAt.append("No Keryx folder has been chosen in the app.")
        }

        if let location = fromLaunchAgent(lookedAt: &lookedAt) {
            return .success(location)
        }
        if let location = await fromLoginShell(&shell, lookedAt: &lookedAt) {
            return .success(location)
        }
        return .failure(LocateFailure(lookedAt: lookedAt))
    }

    // MARK: - The launch agent

    private func fromLaunchAgent(lookedAt: inout [String]) -> KeryxLocation? {
        let path = home + "/" + Self.agentPlist
        guard let data = fileSystem.contents(atPath: path) else {
            lookedAt.append("There is no \(tilde(path)), so the service is not installed.")
            return nil
        }
        guard let plist = (try? PropertyListSerialization.propertyList(from: data, format: nil))
            as? [String: Any]
        else {
            lookedAt.append("\(tilde(path)) could not be read as a property list.")
            return nil
        }
        let arguments = plist["ProgramArguments"] as? [String] ?? []
        guard let program = arguments.first else {
            lookedAt.append("\(tilde(path)) names no program to run.")
            return nil
        }
        let workingDirectory = plist["WorkingDirectory"] as? String
        let declared = (plist["EnvironmentVariables"] as? [String: Any] ?? [:])
            .compactMapValues { $0 as? String }
        let environment = ["HOME": home, "USER": NSUserName()]
            .merging(declared) { _, plist in plist }

        // A `keryx` run directly (a `uv tool install` or a PyPI install): no prefix, and
        // whatever follows it (`serve`) is the service's business, not ours.
        if (program as NSString).lastPathComponent == "keryx" {
            guard fileSystem.isExecutableFile(atPath: program) else {
                lookedAt.append("\(tilde(path)) runs \(tilde(program)), which is not there.")
                return nil
            }
            return KeryxLocation(
                checkout: workingDirectory.flatMap { isKeryxCheckout($0) ? $0 : nil },
                executable: program, prefixArguments: [],
                environment: Sanitise.environment(environment),
                workingDirectory: workingDirectory ?? home, source: .launchAgent)
        }

        guard let checkout = workingDirectory ?? projectArgument(arguments) else {
            lookedAt.append("\(tilde(path)) names no checkout (no WorkingDirectory, no --project).")
            return nil
        }
        guard isKeryxCheckout(checkout) else {
            lookedAt.append("\(tilde(path)) points at \(tilde(checkout)), which has no pyproject.toml naming keryx.")
            return nil
        }
        return forCheckout(checkout, uv: program, environment: environment,
                           source: .launchAgent, lookedAt: &lookedAt)
    }

    /// The value after `--project` (or of `--project=…`), found by name, not position.
    private func projectArgument(_ arguments: [String]) -> String? {
        for (index, argument) in arguments.enumerated() {
            if argument == "--project", index + 1 < arguments.count {
                return arguments[index + 1]
            }
            if argument.hasPrefix("--project=") {
                return String(argument.dropFirst("--project=".count))
            }
        }
        return nil
    }

    // MARK: - The login shell

    private func fromLoginShell(_ shell: inout LoginShell, lookedAt: inout [String]) async -> KeryxLocation? {
        let environment = await shell.environment(runner: runner, base: processEnvironment)
            ?? processEnvironment

        if let path = await commandV(), path.hasPrefix("/"), fileSystem.isExecutableFile(atPath: path) {
            if path.hasSuffix("/.venv/bin/keryx") {
                let checkout = dirname(dirname(dirname(path)))
                return KeryxLocation(
                    checkout: checkout, executable: path, prefixArguments: [],
                    environment: Sanitise.environment(environment),
                    workingDirectory: checkout, source: .loginShell)
            }
            return bare(path, environment: environment)
        }
        lookedAt.append("Your login shell (zsh) has no keryx on its PATH.")

        let uvTool = home + "/.local/bin/keryx"
        if fileSystem.isExecutableFile(atPath: uvTool) {
            return bare(uvTool, environment: environment)
        }
        lookedAt.append("There is no \(tilde(uvTool)) (a uv tool install) either.")
        return nil
    }

    private func bare(_ path: String, environment: [String: String]) -> KeryxLocation {
        KeryxLocation(
            checkout: nil, executable: path, prefixArguments: [],
            environment: Sanitise.environment(environment),
            workingDirectory: home, source: .loginShell)
    }

    /// `command -v keryx` in the login shell: the last line it printed, since an
    /// interactive profile may print a greeting first.
    private func commandV() async -> String? {
        let request = CommandRequest(
            executable: "/bin/zsh", arguments: ["-lic", "command -v keryx"],
            environment: processEnvironment, workingDirectory: home, stdin: Data(),
            timeout: .seconds(5))
        guard let result = try? await runner.run(request), result.exitCode == 0 else { return nil }
        return result.stdoutText
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .last { !$0.isEmpty }
    }

    // MARK: - A checkout

    /// The checkout's own `.venv/bin/keryx` when there is one, else `uv run --project
    /// CHECKOUT --no-sync keryx` (never a plain `uv run`, which may sync the environment).
    private func forCheckout(
        _ checkout: String, uv plistUV: String?, environment: [String: String],
        source: KeryxLocation.Source, lookedAt: inout [String]
    ) -> KeryxLocation? {
        let venv = checkout + "/.venv/bin/keryx"
        let sanitised = Sanitise.environment(environment)
        if fileSystem.isExecutableFile(atPath: venv) {
            return KeryxLocation(
                checkout: checkout, executable: venv, prefixArguments: [],
                environment: sanitised, workingDirectory: checkout, source: source)
        }
        let candidates = [plistUV].compactMap { $0 }
            + [home + "/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv"]
        guard let uv = candidates.first(where: { fileSystem.isExecutableFile(atPath: $0) }) else {
            lookedAt.append("\(tilde(checkout)) has no .venv/bin/keryx, and no uv was found to run it with.")
            return nil
        }
        return KeryxLocation(
            checkout: checkout, executable: uv,
            prefixArguments: ["run", "--project", checkout, "--no-sync", "keryx"],
            environment: sanitised, workingDirectory: checkout, source: source)
    }

    /// True when `folder/pyproject.toml` declares `name = "keryx"` in its `[project]` table.
    func isKeryxCheckout(_ folder: String) -> Bool {
        guard let data = fileSystem.contents(atPath: folder + "/pyproject.toml") else { return false }
        var table = ""
        for rawLine in String(decoding: data, as: UTF8.self).split(whereSeparator: \.isNewline) {
            let line = rawLine.split(separator: "#", maxSplits: 1, omittingEmptySubsequences: false)
                .first.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
            if line.hasPrefix("[") {
                table = line
                continue
            }
            guard table == "[project]" else { continue }
            let parts = line.split(separator: "=", maxSplits: 1).map {
                $0.trimmingCharacters(in: .whitespaces)
            }
            if parts.count == 2, parts[0] == "name" {
                return parts[1] == "\"keryx\"" || parts[1] == "'keryx'"
            }
        }
        return false
    }

    // MARK: - Helpers

    private var processEnvironment: [String: String] {
        ProcessInfo.processInfo.environment.merging(["HOME": home]) { _, home in home }
    }

    private func tilde(_ path: String) -> String {
        path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
    }

    private func dirname(_ path: String) -> String {
        (path as NSString).deletingLastPathComponent
    }
}

/// The login shell's environment, asked for at most once per `locate`.
private struct LoginShell {
    private var fetched = false
    private var cached: [String: String]?

    mutating func environment(runner: CommandRunner, base: [String: String]) async -> [String: String]? {
        if fetched { return cached }
        fetched = true
        let request = CommandRequest(
            executable: "/bin/zsh", arguments: ["-lic", "env -0"], environment: base,
            workingDirectory: base["HOME"], stdin: Data(), timeout: .seconds(5))
        if let result = try? await runner.run(request), result.exitCode == 0 {
            cached = Self.parse(result.stdout)
        }
        return cached
    }

    /// `env -0` output: `KEY=value` entries separated by NUL. Anything an interactive
    /// profile printed before the first entry is dropped, as is any entry whose name is not
    /// a variable name.
    static func parse(_ data: Data) -> [String: String]? {
        var environment: [String: String] = [:]
        for entry in data.split(separator: 0) {
            let text = String(decoding: entry, as: UTF8.self)
            guard let equals = text.firstIndex(of: "=") else { continue }
            var name = Substring(text[..<equals])
            if let newline = name.lastIndex(where: \.isNewline) {
                name = name[name.index(after: newline)...]
            }
            guard let first = name.first, first == "_" || first.isLetter,
                  name.allSatisfy({ $0 == "_" || ($0.isASCII && ($0.isLetter || $0.isNumber)) })
            else { continue }
            environment[String(name)] = String(text[text.index(after: equals)...])
        }
        return environment.isEmpty ? nil : environment
    }
}
