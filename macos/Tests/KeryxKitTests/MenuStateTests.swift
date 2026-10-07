import Foundation
import Testing
@testable import KeryxKit
import KeryxPreview

@Suite struct MenuStateTests {
    static let found = Loadable<KeryxLocation>(value: .fixture)
    static let missing = Loadable<KeryxLocation>(
        error: .notFound(LocateFailure(lookedAt: ["No LaunchAgent."])))

    /// `PreviewData.status` changed by `edit`: running, reachable, nothing to tell.
    static func status(_ edit: (inout StatusReport) -> Void = { _ in }) -> StatusReport {
        var report = PreviewData.status
        report.tasks.running = 0
        report.tasks.unreported = 0
        edit(&report)
        return report
    }

    struct Case: CustomTestStringConvertible, Sendable {
        var name: String
        var location: Loadable<KeryxLocation>
        var status: StatusReport?
        var statusError: KeryxError?
        var expected: MenuState
        var testDescription: String { name }
    }

    static let cases: [Case] = [
        Case(name: "nothing located yet", location: .idle, status: nil, expected: .locating),
        Case(name: "locating", location: Loadable(isLoading: true), status: status(), expected: .locating),
        Case(name: "not found", location: missing, status: nil, expected: .notFound),
        Case(name: "not found wins over a stale status", location: missing, status: status(),
             expected: .notFound),
        Case(name: "migration, from the status", location: found,
             status: status { $0.storage.migrationRequired = true; $0.restart.state = "failed" },
             expected: .needsMigration),
        Case(name: "migration, from the error", location: found, status: nil,
             statusError: .migrationRequired("run keryx migrate"), expected: .needsMigration),
        Case(name: "settings unreadable, before anything read through them", location: found,
             status: status { $0.storage.ok = false; $0.storage.error = "config.toml: bad"
                 $0.service.installed = false; $0.service.active = false
                 $0.health.reachable = false; $0.restart.state = "failed" },
             expected: .attention("Keryx can't read its settings")),
        Case(name: "migration wins over unreadable settings", location: found,
             status: status { $0.storage.ok = false; $0.storage.migrationRequired = true },
             expected: .needsMigration),
        Case(name: "restart failed", location: found,
             status: status { $0.restart.state = "failed"; $0.service.installed = false },
             expected: .attention("Last restart failed")),
        Case(name: "not installed", location: found,
             status: status { $0.service.installed = false; $0.service.active = false
                 $0.health.reachable = false; $0.tasks.unreported = 3 },
             expected: .notInstalled),
        Case(name: "stopped", location: found,
             status: status { $0.service.active = false; $0.health.reachable = false
                 $0.tasks.unreported = 3 },
             expected: .stopped),
        Case(name: "reachable but not installed runs by hand", location: found,
             status: status { $0.service.installed = false; $0.service.active = false },
             expected: .idle),
        Case(name: "results not yet told", location: found,
             status: status { $0.tasks.unreported = 2; $0.tasks.running = 4 },
             expected: .attention("2 results not yet told")),
        Case(name: "one result not yet told", location: found,
             status: status { $0.tasks.unreported = 1 },
             expected: .attention("1 result not yet told")),
        Case(name: "working", location: found, status: status { $0.tasks.running = 3 },
             expected: .working(3)),
        Case(name: "idle", location: found, status: status(), expected: .idle),
        Case(name: "found, status not loaded yet", location: found, status: nil, expected: .locating),
        Case(name: "found, status failed", location: found, status: nil,
             statusError: .timedOut(command: "keryx status"),
             expected: .attention("Keryx Didn't Respond")),
    ]

    @Test(arguments: cases)
    func everyBranch(_ testCase: Case) {
        #expect(menuState(location: testCase.location, status: testCase.status,
                          statusError: testCase.statusError) == testCase.expected)
    }

    @Test func headlines() {
        #expect(MenuState.idle.headline == "Keryx is running")
        #expect(MenuState.working(2).headline == "Keryx is running")
        #expect(MenuState.stopped.headline == "Keryx is stopped")
        #expect(MenuState.notInstalled.headline == "Keryx isn't installed as a service")
        #expect(MenuState.needsMigration.headline == "Keryx needs to move its files")
        #expect(MenuState.notFound.headline == "Keryx isn't on this Mac")
        #expect(MenuState.locating.headline == "Looking for Keryx…")
        #expect(MenuState.attention("Last restart failed").headline == "Last restart failed")
    }

    @Test func symbols() {
        #expect(MenuState.idle.symbol == .plain)
        #expect(MenuState.working(1).symbol == .plain)
        #expect(MenuState.locating.symbol == .plain)
        #expect(MenuState.attention("x").symbol == .dot)
        #expect(MenuState.needsMigration.symbol == .dot)
        #expect(MenuState.stopped.symbol == .slashed)
        #expect(MenuState.notInstalled.symbol == .slashed)
        #expect(MenuState.notFound.symbol == .slashed)
    }

    @Test func sidebarItems() {
        #expect(SidebarItem.allCases == [.overview, .sessions, .voice, .models, .configuration])
        #expect(SidebarItem.allCases.map(\.title) == ["Overview", "Sessions", "Voice", "Local Models", "Configuration"])
        #expect(SidebarItem.allCases.map(\.systemImage) == [
            "gauge.with.dots.needle.33percent", "bubble.left.and.text.bubble.right", "waveform", "cpu",
            "slider.horizontal.3",
        ])
        #expect(SidebarItem.sessions.id == "sessions")
    }

    // MARK: - Loadable

    @Test func loadableKeepsItsValueThroughAFailure() {
        var loadable = Loadable<Int>.idle
        #expect(loadable.value == nil && !loadable.isLoading && loadable.error == nil)
        loadable.begin()
        #expect(loadable.isLoading)
        let now = Date(timeIntervalSince1970: 1_000)
        loadable.succeed(7, at: now)
        #expect(loadable == Loadable(value: 7, updatedAt: now))
        loadable.begin()
        loadable.fail(.timedOut(command: "keryx status"))
        #expect(loadable.value == 7)
        #expect(loadable.error == .timedOut(command: "keryx status"))
        #expect(!loadable.isLoading)
        #expect(loadable.updatedAt == now)
        loadable.succeed(8, at: now)
        #expect(loadable.error == nil)
    }
}

@Suite struct PreferencesTests {
    @Test func keryxFolderIsStoredUnderItsKey() {
        let defaults = freshDefaults()
        let preferences = Preferences(defaults: defaults)
        #expect(preferences.keryxFolder == nil)
        preferences.keryxFolder = "/Users/you/code/keryx"
        #expect(defaults.string(forKey: "keryxFolder") == "/Users/you/code/keryx")
        #expect(Preferences(defaults: defaults).keryxFolder == "/Users/you/code/keryx")
        preferences.keryxFolder = nil
        #expect(defaults.object(forKey: "keryxFolder") == nil)
    }
}
