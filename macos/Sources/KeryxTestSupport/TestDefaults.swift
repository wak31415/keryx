import Foundation

/// A `UserDefaults` of its own for each test, removed again when the test process exits —
/// a suite's domain is a plist in `~/Library/Preferences` once anything is written to it,
/// and a run of the tests must not leave hundreds of them behind.
public enum TestDefaults {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var names: [String] = []
    nonisolated(unsafe) private static var cleanupInstalled = false

    /// A fresh, empty domain, named `keryx-tests-…`.
    public static func fresh() -> UserDefaults {
        let name = "\(prefix)\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defaults.removePersistentDomain(forName: name)
        lock.withLock {
            names.append(name)
            if !cleanupInstalled {
                cleanupInstalled = true
                atexit { TestDefaults.removeAll() }
                removeStale()
            }
        }
        return defaults
    }

    /// What a domain is named with, first.
    static let prefix = "keryx-tests-"

    /// The domains earlier runs left — a test process can end without its `atexit` handlers
    /// running — removed once a run, when they are an hour old: never one a run going on
    /// beside this one is still using.
    static func removeStale(in folder: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Preferences")) {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
        let hourAgo = Date().addingTimeInterval(-3600)
        for name in names where name.hasPrefix(prefix) && name.hasSuffix(".plist") {
            let file = folder.appendingPathComponent(name)
            let modified = (try? file.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
            guard let modified, modified < hourAgo else { continue }
            UserDefaults.standard.removePersistentDomain(forName: String(name.dropLast(".plist".count)))
            try? FileManager.default.removeItem(at: file)
        }
    }

    /// Removes every domain `fresh` made.
    static func removeAll() {
        let all = lock.withLock { names }
        for name in all {
            UserDefaults.standard.removePersistentDomain(forName: name)
        }
        CFPreferencesAppSynchronize(kCFPreferencesCurrentApplication)
    }
}
