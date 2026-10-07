import Foundation

/// The three questions the locator asks of the disk, behind a protocol so its tests never
/// read the real `~/Library/LaunchAgents` or a real checkout.
public protocol FileSystem: Sendable {
    /// The file's bytes, or `nil` when it does not exist or cannot be read.
    func contents(atPath path: String) -> Data?
    /// True for a regular file this process may execute (not a directory).
    func isExecutableFile(atPath path: String) -> Bool
}

public struct LocalFileSystem: FileSystem {
    public init() {}

    public func contents(atPath path: String) -> Data? {
        FileManager.default.contents(atPath: path)
    }

    public func isExecutableFile(atPath path: String) -> Bool {
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
              !isDirectory.boolValue
        else { return false }
        return FileManager.default.isExecutableFile(atPath: path)
    }
}

/// A test double: `files` is what can be read, `executables` what can be run.
public struct FakeFileSystem: FileSystem {
    public var files: [String: Data]
    public var executables: Set<String>

    public init(files: [String: Data] = [:], executables: Set<String> = []) {
        self.files = files
        self.executables = executables
    }

    public func contents(atPath path: String) -> Data? { files[path] }
    public func isExecutableFile(atPath path: String) -> Bool { executables.contains(path) }
}
