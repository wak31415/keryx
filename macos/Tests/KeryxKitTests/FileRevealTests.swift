import Foundation
import Testing
@testable import KeryxKit

/// What Show Log and Show Full Report may reveal: a regular file of the right kind inside
/// Keryx's own folder, and nothing a message's text could point elsewhere.
@Suite struct FileRevealTests {
    /// A scratch folder: `state/logs`, `data/tasks`, and things around them.
    final class Scratch {
        let root: String

        init() throws {
            root = (NSTemporaryDirectory() as NSString).appendingPathComponent("keryx-reveal-\(UUID().uuidString)")
            let files = FileManager.default
            for folder in ["state/logs", "data/tasks", "elsewhere", "state/logs/Evil.app", "data/tasks/run.log"] {
                try files.createDirectory(atPath: path(folder), withIntermediateDirectories: true)
            }
            for file in ["state/logs/keryx-llm.err.log", "state/logs/run.command", "data/tasks/12.md",
                         "data/tasks/12.txt", "elsewhere/other.log", "elsewhere/report.md"] {
                #expect(files.createFile(atPath: path(file), contents: Data("x".utf8)))
            }
            try files.createSymbolicLink(atPath: path("state/logs/escape.log"),
                                         withDestinationPath: path("elsewhere/other.log"))
        }

        deinit { try? FileManager.default.removeItem(atPath: root) }

        func path(_ relative: String) -> String { (root as NSString).appendingPathComponent(relative) }

        var paths: Paths {
            Paths(ok: true, error: nil, config: path("config"), data: path("data"), state: path("state"),
                  cache: path("cache"), logs: path("state/logs"), memory: path("data/memory.md"))
        }
    }

    @Test func aLogInTheLogsFolderIsRevealed() throws {
        let scratch = try Scratch()
        let url = FileReveal.log(scratch.path("state/logs/keryx-llm.err.log"), paths: scratch.paths)
        #expect(url?.lastPathComponent == "keryx-llm.err.log")
    }

    @Test func anythingElseTheMessageNamesIsNot() throws {
        let scratch = try Scratch()
        let paths = scratch.paths
        // Outside the logs folder, or out of it by `..` or a link.
        #expect(FileReveal.log(scratch.path("elsewhere/other.log"), paths: paths) == nil)
        #expect(FileReveal.log(scratch.path("state/logs/../../elsewhere/other.log"), paths: paths) == nil)
        #expect(FileReveal.log(scratch.path("state/logs/escape.log"), paths: paths) == nil)
        // Not a log: a script, a bundle, a folder named like a log, nothing there.
        #expect(FileReveal.log(scratch.path("state/logs/run.command"), paths: paths) == nil)
        #expect(FileReveal.log(scratch.path("state/logs/Evil.app"), paths: paths) == nil)
        #expect(FileReveal.log(scratch.path("data/tasks/run.log"), paths: paths) == nil)
        #expect(FileReveal.log(scratch.path("state/logs/missing.log"), paths: paths) == nil)
        // Not a path at all, or before the status says where the folders are.
        #expect(FileReveal.log("keryx-llm.err.log", paths: paths) == nil)
        #expect(FileReveal.log(scratch.path("state/logs/keryx-llm.err.log"), paths: nil) == nil)
        // A folder whose name only starts like the logs folder's.
        #expect(FileReveal.checked(scratch.path("state/logs/keryx-llm.err.log"),
                                   under: [scratch.path("state/lo")], extensions: ["log"]) == nil)
    }

    @Test func aReportIsMarkdownOrTextInTheDataFolder() throws {
        let scratch = try Scratch()
        let paths = scratch.paths
        #expect(FileReveal.report(scratch.path("data/tasks/12.md"), paths: paths) != nil)
        #expect(FileReveal.report(scratch.path("data/tasks/12.txt"), paths: paths) != nil)
        #expect(FileReveal.report(scratch.path("elsewhere/report.md"), paths: paths) == nil)
        #expect(FileReveal.report(scratch.path("state/logs/keryx-llm.err.log"), paths: paths) == nil)
    }
}
