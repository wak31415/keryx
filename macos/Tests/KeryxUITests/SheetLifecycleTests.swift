import Foundation
import Testing

/// The sheets that hold a secret, read as source: what happens when one goes while it is
/// still asking or saving. Views cannot be driven here, so these pin down the structure the
/// rules rest on — a save keeps its sheet up, a check is stopped when its sheet goes, and a
/// failed save forgets what was typed.
@Suite struct SheetLifecycleTests {
    static func source(_ relative: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(contentsOf: root.appendingPathComponent("Sources/KeryxUI/\(relative)"), encoding: .utf8)
    }

    static let sheets = ["PIN/PinSheet.swift", "Configuration/SecretSheet.swift", "Phone/TwilioAccountSheet.swift"]

    @Test(arguments: sheets)
    func aSaveKeepsItsSheetUp(_ file: String) throws {
        let text = try Self.source(file)
        #expect(text.contains(".interactiveDismissDisabled(saving)"))
        // Its Cancel is off while it saves, too.
        let cancel = try #require(text.range(of: #"Button("Cancel", role: .cancel) { close() }"#))
        #expect(text[cancel.upperBound...].prefix(200).contains(".disabled(saving)"))
    }

    @Test(arguments: ["Configuration/SecretSheet.swift", "Phone/TwilioAccountSheet.swift"])
    func aCheckIsStoppedWhenItsSheetGoes(_ file: String) throws {
        let text = try Self.source(file)
        #expect(text.contains("verifying = Task {"), "the check is held")
        #expect(text.contains("verifying?.cancel()"))
        let disappear = try #require(text.range(of: ".onDisappear {"))
        #expect(text[disappear.upperBound...].prefix(120).contains("stopVerifying()"))
        let close = try #require(text.range(of: "private func close() {"))
        #expect(text[close.upperBound...].prefix(80).contains("stopVerifying()"))
    }

    @Test func aPinSaveIsStoppedIfItsSheetGoesWithItsWindow() throws {
        let text = try Self.source("PIN/PinSheet.swift")
        #expect(text.contains("saveTask = Task {"))
        let disappear = try #require(text.range(of: ".onDisappear {"))
        #expect(text[disappear.upperBound...].prefix(200).contains("saveTask?.cancel()"))
    }

    @Test func aFailedTwilioSaveForgetsTheToken() throws {
        let text = try Self.source("Phone/TwilioAccountSheet.swift")
        let save = try #require(text.range(of: "private func save() {"))
        let end = try #require(text.range(of: "private func forgetToken()"))
        let body = text[save.upperBound..<end.lowerBound]
        // Both ways a save fails (the SID, then the token) empty the token.
        #expect(body.components(separatedBy: "forgetToken()").count - 1 == 2)
        #expect(text.contains("token = \"\"\n        check = .unchecked"))
    }
}
