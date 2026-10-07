import Foundation

/// A file the app may show in the Finder: never a path taken on trust from text the CLI
/// printed, only a regular file of an expected kind inside one of Keryx's own folders. The
/// app reveals it (selects it in a Finder window); it never opens it, so a path that named an
/// app or a script could not launch one.
public enum FileReveal {
    /// `path` with symlinks and `..` resolved, when it is a regular file whose extension is
    /// one of `extensions` (lowercase, without the dot) inside one of `roots`; `nil` for
    /// anything else — a folder, a bundle, a link out of the roots, a file not there.
    public static func checked(_ path: String, under roots: [String], extensions: Set<String>) -> URL? {
        guard !path.isEmpty, path.hasPrefix("/") else { return nil }
        let url = URL(fileURLWithPath: path).resolvingSymlinksInPath().standardizedFileURL
        guard extensions.contains(url.pathExtension.lowercased()),
              let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
              attributes[.type] as? FileAttributeType == .typeRegular else { return nil }
        let inside = roots.contains { root in
            guard !root.isEmpty, root.hasPrefix("/") else { return false }
            let folder = URL(fileURLWithPath: root).resolvingSymlinksInPath().standardizedFileURL.path
            return url.path.hasPrefix(folder.hasSuffix("/") ? folder : folder + "/")
        }
        return inside ? url : nil
    }

    /// A server's log the CLI named: a `.log` file in Keryx's logs folder.
    public static func log(_ path: String, paths: Paths?) -> URL? {
        guard let paths else { return nil }
        return checked(path, under: [paths.logs], extensions: ["log"])
    }

    /// A task's report: a Markdown or text file in Keryx's data folder.
    public static func report(_ path: String, paths: Paths?) -> URL? {
        guard let paths else { return nil }
        return checked(path, under: [paths.data], extensions: ["md", "txt"])
    }
}
