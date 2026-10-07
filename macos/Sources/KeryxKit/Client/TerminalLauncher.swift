import AppKit
import Foundation

/// Runs a TTY-only `keryx` command (`setup`, `migrate`, `auth login`) in Terminal: a
/// `.command` file that sets up what the service would see and `exec`s the command, opened
/// with `NSWorkspace`. No AppleScript — no Automation prompt, and nothing interpolated into
/// script text that is not single-quoted. No secret is ever written into it: only the
/// variables that say where Keryx's files are, and where to find programs.
public enum TerminalLauncher {
    static let exported = [
        "KERYX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
        "PATH", "HOME",
    ]

    /// A script left behind (Terminal opened it but never ran it) is removed once it is this
    /// old: it holds no secret, but it does hold paths.
    static let staleAfter: TimeInterval = 10 * 60

    /// The app's own folder for the scripts, `keryx-commands` in NSTemporaryDirectory
    /// (0700), so the stale-script sweep lists that and nothing else.
    public static var commandsDirectory: String {
        (NSTemporaryDirectory() as NSString).appendingPathComponent("keryx-commands")
    }

    /// `commandsDirectory` (under `base`), made 0700 if it is not there; refused if what is
    /// there is not a directory of this user's (a symlink, a file).
    static func preparedCommandsDirectory(in base: String = NSTemporaryDirectory()) throws -> String {
        let directory = (base as NSString).appendingPathComponent("keryx-commands")
        let files = FileManager.default
        if mkdir(directory, 0o700) != 0 && errno != EEXIST {
            throw KeryxError.launch("Could not make \(directory) for the script for Terminal.")
        }
        var info = stat()
        guard lstat(directory, &info) == 0, info.st_mode & S_IFMT == S_IFDIR, info.st_uid == getuid()
        else {
            throw KeryxError.launch("\(directory) is not a folder of yours, so no script was written there.")
        }
        try? files.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory)
        return directory
    }

    /// The text of a .command file that runs `keryx <arguments>` in the checkout (or `/`,
    /// with none) with the resolved KERYX_HOME / XDG_* / PATH / HOME exported, every value
    /// single-quoted, and each of those the location does not set unset — so Terminal's own
    /// value (its shell's `KERYX_HOME`, say) cannot stand in for one the service never had.
    public static func script(for location: KeryxLocation, arguments: [String]) -> String {
        script(for: location, argv: [location.executable] + location.prefixArguments + arguments)
    }

    private static func script(for location: KeryxLocation, argv: [String]) -> String {
        var lines = [
            "#!/bin/sh",
            // Delete the file first: it has done its job once the shell has read it.
            #"rm -f -- "$0""#,
            // The owner at a terminal, never the service.
            "unset KERYX_ACTOR JARVIS_ACTOR",
        ]
        for name in exported {
            if let value = location.environment[name] {
                lines.append("export \(name)=\(quote(value))")
            } else {
                lines.append("unset \(name)")
            }
        }
        lines.append("cd \(quote(location.runDirectory)) || exit 1")
        lines.append("exec " + argv.map(quote).joined(separator: " "))
        return lines.joined(separator: "\n") + "\n"
    }

    /// Writes it (0700, in `commandsDirectory`, name keryx-<uuid>.command; the script deletes
    /// itself first) and opens it with NSWorkspace. Throws on write failure. Any script an
    /// earlier call left behind is removed first (`removeStaleScripts`).
    @MainActor public static func open(_ location: KeryxLocation, arguments: [String]) throws {
        try open(script(for: location, arguments: arguments))
    }

    @MainActor private static func open(_ text: String) throws {
        removeStaleScripts()
        let path = (try preparedCommandsDirectory() as NSString)
            .appendingPathComponent("keryx-\(UUID().uuidString).command")
        let contents = Data(text.utf8)
        guard FileManager.default.createFile(
            atPath: path, contents: contents, attributes: [.posixPermissions: 0o700])
        else {
            throw KeryxError.terminal("Couldn't write the script for Terminal to \(path).")
        }
        guard NSWorkspace.shared.open(URL(fileURLWithPath: path)) else {
            try? FileManager.default.removeItem(atPath: path)
            throw KeryxError.terminal("Terminal couldn't be opened. Run the command in Terminal yourself.")
        }
    }

    /// Removes every `keryx-<uuid>.command` in `directory` (`commandsDirectory`, the app's
    /// own) last changed more than `staleAfter` before `now`: a script whose Terminal never
    /// ran it, so never deleted itself. Run at launch, with each status poll (off the main
    /// actor), and before each open.
    public static func removeStaleScripts(in directory: String = commandsDirectory, now: Date = Date()) {
        let files = FileManager.default
        guard let names = try? files.contentsOfDirectory(atPath: directory) else { return }
        for name in names where isScriptName(name) {
            let path = (directory as NSString).appendingPathComponent(name)
            guard let attributes = try? files.attributesOfItem(atPath: path),
                  attributes[.type] as? FileAttributeType == .typeRegular,
                  let modified = attributes[.modificationDate] as? Date,
                  now.timeIntervalSince(modified) > staleAfter
            else { continue }
            try? files.removeItem(atPath: path)
        }
    }

    /// `keryx-<uuid>.command`, as `open` names them, and nothing else.
    static func isScriptName(_ name: String) -> Bool {
        guard name.hasPrefix("keryx-"), name.hasSuffix(".command") else { return false }
        let middle = name.dropFirst("keryx-".count).dropLast(".command".count)
        return UUID(uuidString: String(middle)) != nil
    }

    /// POSIX single quotes: `it's` becomes `'it'"'"'s'`.
    public static func quote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: #"'"'"'"#) + "'"
    }
}
