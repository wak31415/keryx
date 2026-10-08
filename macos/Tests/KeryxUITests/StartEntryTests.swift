import Foundation
import Testing

/// Every screen that starts Keryx goes the one way the app starts it — `AppModel`'s
/// `startAnswering` (a click) or `ensureAnswering` (by itself) — which keeps the rule: never
/// for a sandbox run, never before setup allows, never beside a Keryx already answering, a
/// start under way joined. Nothing in the UI module can reach `installService` (it is
/// internal to KeryxKit); this holds the callers themselves to it, by source.
@Suite struct StartEntryTests {
    static func source(_ path: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Sources/KeryxUI")
        return try String(contentsOf: root.appendingPathComponent(path), encoding: .utf8)
    }

    @Test func checkingTheChecklistAndTheOverviewStartKeryxTheOneWay() throws {
        let checking = try Self.source("Setup/Wizard/FinishFrame.swift")
        #expect(checking.contains("await model.ensureAnswering()"), "Checking, by itself")
        #expect(checking.contains("await model.startAnswering()"), "Checking's Start Keryx and Try Again")
        #expect(try Self.source("Kit/CheckLine.swift").contains("await model.startAnswering()"), "the checklist")
        #expect(try Self.source("Overview/OverviewView.swift").contains("await model.startAnswering()"), "Start")
        for path in ["Setup/Wizard/FinishFrame.swift", "Setup/Wizard/WizardFlow.swift", "Kit/CheckLine.swift",
                     "Overview/OverviewView.swift"] {
            let text = try Self.source(path)
            #expect(!text.contains("serviceInstallable"), "\(path): a start rule of its own")
            #expect(!text.contains(".installService("), "\(path): an install of its own")
        }
    }
}
